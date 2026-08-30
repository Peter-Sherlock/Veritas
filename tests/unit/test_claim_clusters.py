from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from veritas.aggregation import (
    ClaimClusterStore,
    ClaimClusterStoreError,
    ClusterPolicy,
    similarity,
)
from veritas.aggregation.clusterer import number_tokens, statement_tokens
from veritas.extraction.pipeline import derive_canonical_key


class SimilarityGuardTests(unittest.TestCase):
    def test_identical_statements_score_one(self) -> None:
        self.assertEqual(
            1.0, similarity("HTTPX does not follow redirects by default.", "HTTPX does not follow redirects by default.")
        )

    def test_number_guard_keeps_versioned_facts_apart(self) -> None:
        self.assertIsNone(
            similarity(
                "HTTPX requires Python 3.7 or later.",
                "HTTPX requires Python 3.8 or later.",
            )
        )
        self.assertIsNone(
            similarity(
                "HTTPX 0.24.1 requires Python 3.7 or later",
                "HTTPX requires Python 3.7 or later",
            )
        )

    def test_missing_number_is_guarded(self) -> None:
        self.assertIsNone(
            similarity(
                "Support for SSLKEYLOGFILE requires Python 3.8 and OpenSSL 1.1.1 or newer.",
                "Support for SSLKEYLOGFILE requires Python 3.8 or newer.",
            )
        )

    def test_negation_guard_keeps_opposites_apart(self) -> None:
        self.assertIsNone(
            similarity(
                "HTTPX follows redirects by default.",
                "HTTPX does not follow redirects by default.",
            )
        )

    def test_dotted_versions_stay_one_token(self) -> None:
        tokens = statement_tokens("requires Python 3.8 and OpenSSL 1.1.1")
        self.assertIn("3.8", tokens)
        self.assertIn("1.1.1", tokens)
        self.assertEqual(frozenset({"3.8", "1.1.1"}), number_tokens(tokens))

    def test_empty_statement_is_guarded(self) -> None:
        self.assertIsNone(similarity("", "HTTPX retries failures."))


class ClaimClusterStoreTests(unittest.TestCase):
    def test_founder_then_lexical_join_is_stable_and_idempotent(self) -> None:
        founder = "HTTPX retries connection setup failures."
        paraphrase = "HTTPX automatically retries connection setup failures."
        founder_key = derive_canonical_key(founder)
        paraphrase_key = derive_canonical_key(paraphrase)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clusters.sqlite3"
            with ClaimClusterStore(path) as store:
                first = store.resolve(
                    canonical_key=founder_key, statement=founder, observed_at="2026-08-30T00:00:00Z"
                )
                self.assertEqual("founder", first.method)
                self.assertFalse(first.merged)
                second = store.resolve(
                    canonical_key=paraphrase_key,
                    statement=paraphrase,
                    observed_at="2026-08-30T00:00:00Z",
                )
                self.assertEqual("lexical", second.method)
                self.assertTrue(second.merged)
                self.assertEqual(first.representative_key, second.representative_key)
                # Re-resolution is idempotent and returns the stored mapping.
                again = store.resolve(
                    canonical_key=paraphrase_key,
                    statement=paraphrase,
                    observed_at="2026-08-30T00:00:00Z",
                )
                self.assertEqual(second, again)
                self.assertEqual(
                    (paraphrase_key, founder_key), store.find_cluster(paraphrase_key)
                )
                self.assertEqual({"clusters": 1, "members": 2}, store.counts())

            # The mapping and the frozen representative survive a reopen.
            with ClaimClusterStore(path) as reopened:
                self.assertEqual(
                    founder_key, reopened.representative_key(paraphrase_key)
                )
                self.assertEqual({"clusters": 1, "members": 2}, reopened.counts())

    def test_best_scoring_cluster_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with ClaimClusterStore(Path(tmp) / "clusters.sqlite3") as store:
                alpha = "The alpha feature requires a beta flag."
                beta = "The alpha feature requires a gamma flag."
                for statement in (alpha, beta):
                    store.resolve(
                        canonical_key=derive_canonical_key(statement),
                        statement=statement,
                        observed_at="2026-08-30T00:00:00Z",
                    )
                target = "The alpha feature requires the beta flag."
                resolution = store.resolve(
                    canonical_key=derive_canonical_key(target),
                    statement=target,
                    observed_at="2026-08-30T00:00:00Z",
                )
                self.assertEqual(derive_canonical_key(alpha), resolution.representative_key)

    def test_statement_below_threshold_founds_new_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with ClaimClusterStore(Path(tmp) / "clusters.sqlite3") as store:
                first = "HTTPX retries connection setup failures."
                other = "Timeouts apply to every request by default."
                store.resolve(
                    canonical_key=derive_canonical_key(first),
                    statement=first,
                    observed_at="2026-08-30T00:00:00Z",
                )
                second = store.resolve(
                    canonical_key=derive_canonical_key(other),
                    statement=other,
                    observed_at="2026-08-30T00:00:00Z",
                )
                self.assertFalse(second.merged)
                self.assertEqual({"clusters": 2, "members": 2}, store.counts())

    def test_reopening_with_a_different_threshold_is_policy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clusters.sqlite3"
            with ClaimClusterStore(path):
                pass
            with self.assertRaises(ClaimClusterStoreError) as caught:
                ClaimClusterStore(path, policy=ClusterPolicy(min_jaccard=0.5))
            self.assertEqual("policy_drift", caught.exception.code)

    def test_schema_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clusters.sqlite3"
            with ClaimClusterStore(path):
                pass
            raw = sqlite3.connect(path)
            try:
                raw.execute(
                    "UPDATE store_meta SET value = 'claim-clusters-0' WHERE key = 'schema'"
                )
                raw.commit()
            finally:
                raw.close()
            with self.assertRaises(ClaimClusterStoreError) as caught:
                ClaimClusterStore(path)
            self.assertEqual("schema_drift", caught.exception.code)

    def test_key_mismatch_and_empty_statement_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with ClaimClusterStore(Path(tmp) / "clusters.sqlite3") as store:
                with self.assertRaises(ClaimClusterStoreError) as caught:
                    store.resolve(
                        canonical_key="wrong_key",
                        statement="HTTPX retries failures.",
                        observed_at="2026-08-30T00:00:00Z",
                    )
                self.assertEqual("canonical_key_mismatch", caught.exception.code)
                with self.assertRaises(ClaimClusterStoreError) as caught:
                    store.resolve(
                        canonical_key="x", statement="   ", observed_at="2026-08-30T00:00:00Z"
                    )
                self.assertEqual("invalid_statement", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
