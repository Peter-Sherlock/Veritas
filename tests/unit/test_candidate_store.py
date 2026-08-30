from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from veritas.extraction.pipeline import derive_canonical_key
from veritas.extraction.store import (
    CANDIDATE_STORE_SCHEMA,
    CandidateRecord,
    CandidateStore,
    CandidateStoreError,
    candidate_content_hash,
    candidate_id_for,
)


def _record(
    statement: str,
    *,
    relation: str = "supports",
    quote: str = "HTTPX supports HTTP/2.",
    doc: str = "quickstart",
    version: str = "0.28.0",
    canonical_key: str | None = None,
) -> CandidateRecord:
    return CandidateRecord(
        source_version_id=f"httpx-m1-2c:{doc}@{version}",
        doc_id=doc,
        version_id=version,
        canonical_key=canonical_key
        if canonical_key is not None
        else derive_canonical_key(statement),
        statement=statement,
        relation=relation,
        quote=quote,
        char_start=0,
        char_end=len(quote),
    )


class CandidateStoreTests(unittest.TestCase):
    def test_persist_is_idempotent_across_runs(self) -> None:
        records = [
            _record("HTTPX retries connection setup failures."),
            _record("HTTP/2 must be enabled explicitly.", doc="http2"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with CandidateStore(Path(tmp) / "candidates.db") as store:
                first = store.persist(records, run_id="run-1", observed_at="2026-08-29T00:00:00Z")
                self.assertEqual(
                    {"seen": 2, "persisted": 2, "deduped": 0, "observations_new": 2},
                    first,
                )
                second = store.persist(records, run_id="run-1", observed_at="2026-08-29T00:00:00Z")
                self.assertEqual(
                    {"seen": 2, "persisted": 0, "deduped": 2, "observations_new": 0},
                    second,
                )
                # A different run observing the same candidates dedups content
                # but records its own observation.
                third = store.persist(records, run_id="run-2", observed_at="2026-08-30T00:00:00Z")
                self.assertEqual(
                    {"seen": 2, "persisted": 0, "deduped": 2, "observations_new": 2},
                    third,
                )
                self.assertEqual(
                    {"candidates": 2, "observations": 4, "distinct_canonical_keys": 2},
                    store.counts(),
                )

    def test_candidate_identity_is_deterministic(self) -> None:
        content_hash = candidate_content_hash(
            statement="HTTPX retries connection setup failures.",
            relation="supports",
            quote="HTTPX supports HTTP/2.",
        )
        candidate_id = candidate_id_for(
            source_version_id="httpx-m1-2c:quickstart@0.28.0",
            canonical_key="httpx_retries_connection_setup_failures",
            content_hash=content_hash,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with CandidateStore(Path(tmp) / "candidates.db") as store:
                store.persist(
                    [_record("HTTPX retries connection setup failures.")],
                    run_id="run-1",
                    observed_at="2026-08-29T00:00:00Z",
                )
                rows = store.list_candidates_for_key("httpx_retries_connection_setup_failures")
                self.assertEqual(1, len(rows))
                self.assertEqual(candidate_id, rows[0]["candidate_id"])
                self.assertEqual(content_hash, rows[0]["content_hash"])

    def test_cosmetic_variant_stays_a_distinct_candidate_under_one_key(self) -> None:
        original = _record("HTTP/2 must be enabled explicitly.")
        reworded = _record(
            "http/2 must be enabled explicitly",
            doc="http2",
            canonical_key=original.canonical_key,
        )
        self.assertEqual(original.canonical_key, reworded.canonical_key)
        self.assertNotEqual(
            candidate_content_hash(
                statement=original.statement,
                relation=original.relation,
                quote=original.quote,
            ),
            candidate_content_hash(
                statement=reworded.statement,
                relation=reworded.relation,
                quote=reworded.quote,
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with CandidateStore(Path(tmp) / "candidates.db") as store:
                store.persist(
                    [original, reworded], run_id="run-1", observed_at="2026-08-29T00:00:00Z"
                )
                rows = store.list_candidates_for_key(original.canonical_key)
                self.assertEqual(2, len(rows))
                self.assertEqual(
                    {original.statement, reworded.statement},
                    {row["statement"] for row in rows},
                )

    def test_relation_conflict_is_persisted_and_surfaced(self) -> None:
        supports = _record("HTTPX retries connection setup failures.")
        contradicts = _record(
            "HTTPX retries connection setup failures",
            relation="contradicts",
            quote="HTTPX never retries.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with CandidateStore(Path(tmp) / "candidates.db") as store:
                store.persist(
                    [supports, contradicts], run_id="run-1", observed_at="2026-08-29T00:00:00Z"
                )
                self.assertEqual(2, store.counts()["candidates"])
                conflicts = store.list_relation_conflicts()
                self.assertEqual(1, len(conflicts))
                self.assertEqual(supports.source_version_id, conflicts[0]["source_version_id"])
                self.assertEqual(supports.canonical_key, conflicts[0]["canonical_key"])
                self.assertEqual(2, conflicts[0]["relation_count"])

    def test_mismatched_derived_key_rejects_the_whole_batch(self) -> None:
        valid = _record("HTTPX retries connection setup failures.")
        forged = _record(
            "HTTP/2 must be enabled explicitly.",
            doc="http2",
            canonical_key="hand_written_key",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with CandidateStore(Path(tmp) / "candidates.db") as store:
                with self.assertRaises(CandidateStoreError) as caught:
                    store.persist(
                        [valid, forged], run_id="run-1", observed_at="2026-08-29T00:00:00Z"
                    )
                self.assertEqual("canonical_key_mismatch", caught.exception.code)
                # No partial rows: the failed batch must roll back entirely.
                self.assertEqual(
                    {"candidates": 0, "observations": 0, "distinct_canonical_keys": 0},
                    store.counts(),
                )
                # After the rejected batch the store still accepts valid data.
                store.persist(
                    [valid], run_id="run-1", observed_at="2026-08-29T00:00:00Z"
                )
                self.assertEqual(1, store.counts()["candidates"])

    def test_invalid_relation_and_empty_content_are_rejected(self) -> None:
        cases = [
            (_record("A statement.", relation="maybe"), "invalid_relation"),
            (_record("A statement.", quote="   "), "invalid_candidate"),
            (_record("   .", quote="q"), "invalid_candidate"),
        ]
        for record, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as tmp:
                    with CandidateStore(Path(tmp) / "candidates.db") as store:
                        with self.assertRaises(CandidateStoreError) as caught:
                            store.persist(
                                [record], run_id="run-1", observed_at="2026-08-29T00:00:00Z"
                            )
                        self.assertEqual(expected_code, caught.exception.code)

    def test_schema_drift_guard_rejects_unknown_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.db"
            with CandidateStore(path) as store:
                store.connection.execute(
                    "UPDATE store_meta SET value = 'extraction-candidates-0' "
                    "WHERE key = 'schema_id'"
                )
            with self.assertRaises(CandidateStoreError) as caught:
                CandidateStore(path)
            self.assertEqual("schema_drift", caught.exception.code)

    def test_store_declares_its_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with CandidateStore(Path(tmp) / "candidates.db") as store:
                row = store.connection.execute(
                    "SELECT value FROM store_meta WHERE key = 'schema_id'"
                ).fetchone()
                self.assertEqual(CANDIDATE_STORE_SCHEMA, row["value"])


if __name__ == "__main__":
    unittest.main()
