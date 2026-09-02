from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from veritas.evaluation import evolution_benchmark as bench
from veritas.evaluation.evolution_benchmark import (
    BENCHMARK_REVISIONS,
    BenchmarkError,
    assert_equivalent,
    full_recompute_state,
    run_benchmark,
    validate_plan,
    write_summary,
)
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.integration import GraphBridge
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"
COMMITTED_SUMMARY = REPO_ROOT / "artifacts" / "evolution" / "m1-5b-benchmark" / "summary.json"

# Frozen evidence from the committed artifact (D-038); the assertions pin the
# measured cost of selective evolution on the real corpus history.
EXPECTED_KINDS = (
    "survival_revision",
    "survival_revision",
    "survival_revision",
    "watched_fact_removed",  # index: Python floor 3.7+ -> 3.8+
    "survival_revision",
    "survival_revision",
    "survival_revision",
    "survival_revision",
    "survival_revision",
    "survival_revision",
    "watched_fact_removed",  # quickstart: no-decoding sentence removed
    "watched_fact_removed",  # compatibility: REQUESTS_CA_BUNDLE replaced
    "watched_fact_removed",  # environment_variables: SSLKEYLOGFILE section removed
)


class EvolutionBenchmarkM15BTests(unittest.TestCase):
    def test_scaled_real_history_timeline_matches_full_recompute_at_every_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_benchmark(CORPUS_ROOT, Path(tmp) / "benchmark.sqlite3")
        self.assertEqual("m1-5b-evolution-benchmark", summary["benchmark_id"])
        self.assertEqual("httpx-docs", summary["corpus_id"])

        t0 = summary["t0"]
        self.assertEqual(
            {
                "as_of": "2023-06-01T00:00:00Z",
                "source_versions": 8,
                "claims": 6,
                "evidence_spans": 6,
                "dependency_edges": 14,
                "conclusions": 7,
            },
            t0,
        )

        events = summary["timeline"]["events"]
        self.assertEqual(len(BENCHMARK_REVISIONS), summary["timeline"]["event_count"])
        self.assertEqual(13, len(events))
        self.assertEqual(
            list(EXPECTED_KINDS), [event["kind"] for event in events]
        )
        self.assertTrue(all(event["equivalent"] for event in events))
        # Real published_at ordering: the six 0.25.2 revisions share one
        # instant, so doc_id breaks the tie; 0.27.2 revisions land in 2024.
        self.assertEqual(
            [
                "2023-11-24T16:33:18+04:00",
                "2023-11-24T16:33:18+04:00",
                "2023-11-24T16:33:18+04:00",
                "2023-11-24T16:33:18+04:00",
                "2023-11-24T16:33:18+04:00",
                "2023-12-20T14:52:22+04:00",
                "2023-12-20T14:52:22+04:00",
                "2024-08-27T13:52:05+01:00",
                "2024-08-27T13:52:05+01:00",
                "2024-08-27T13:52:05+01:00",
                "2024-08-27T13:52:05+01:00",
                "2024-12-06T15:35:41Z",
                "2024-12-06T15:35:41Z",
            ],
            [event["effective_at"] for event in events],
        )

        aggregate = summary["aggregate"]
        self.assertEqual(4, aggregate["semantic_claim_changes"])
        self.assertEqual(9, aggregate["rechecked_unchanged_claims"])
        self.assertEqual(6, aggregate["conclusion_recomputations"])
        self.assertEqual(5, aggregate["conclusion_versions_created"])
        # The frozen cost claim fulfilling Gate P0 condition 2: selective
        # evolution performed 23 rule evaluations where a full recompute of
        # the whole graph would have performed 185.
        self.assertEqual(23, aggregate["selective_evaluations"])
        self.assertEqual(185, aggregate["full_recompute_evaluations"])
        self.assertAlmostEqual(23 / 185, aggregate["cost_ratio"])
        self.assertLess(aggregate["cost_ratio"], 0.2)
        self.assertTrue(aggregate["equivalent_at_every_event"])

        final = summary["final_state"]
        self.assertEqual(10, final["claims"])
        self.assertEqual(
            {
                "advanced_fact_supported": "pass",
                "async_fact_supported": "pass",
                "compatibility_fact_supported": "unknown",
                "environment_variables_fact_supported": "unknown",
                "index_fact_supported": "unknown",
                "python_floor_claims_supported": "unknown",
                "quickstart_fact_supported": "unknown",
            },
            final["conclusion_outcomes"],
        )
        self.assertEqual(
            {
                "advanced": "accepted",
                "async": "accepted",
                "compatibility": "unsupported",
                "environment_variables": "unsupported",
                "index": "unsupported",
                "quickstart": "unsupported",
            },
            final["watched_claim_states"],
        )

    def test_benchmark_is_deterministic_across_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = run_benchmark(CORPUS_ROOT, Path(tmp) / "one.sqlite3")
            second = run_benchmark(CORPUS_ROOT, Path(tmp) / "two.sqlite3")
        self.assertEqual(first, second)

    def test_committed_summary_artifact_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "summary.json"
            summary = run_benchmark(CORPUS_ROOT, Path(tmp) / "benchmark.sqlite3")
            write_summary(summary, output)
            self.assertEqual(
                COMMITTED_SUMMARY.read_bytes(), output.read_bytes()
            )

    def test_plan_validation_rejects_quotes_absent_from_corpus(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        tampered = tuple(
            doc if doc.doc_id != "index" else bench.BenchmarkDoc(
                doc_id=doc.doc_id,
                question=doc.question,
                query=doc.query,
                watched=(
                    bench.WatchedAssertion(
                        "0.24.1",
                        "HTTPX requires Python 3.9+",
                        "HTTPX requires Python 3.9 or later.",
                    ),
                    doc.watched[1],
                ),
            )
            for doc in bench.BENCHMARK_DOCS
        )
        with mock.patch.object(bench, "BENCHMARK_DOCS", tampered):
            with self.assertRaises(BenchmarkError) as caught:
                validate_plan(corpus)
        self.assertEqual("quote_not_in_corpus", caught.exception.code)

    def test_plan_validation_rejects_versions_outside_corpus_history(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        tampered = tuple(
            doc if doc.doc_id != "index" else bench.BenchmarkDoc(
                doc_id=doc.doc_id,
                question=doc.question,
                query=doc.query,
                watched=(
                    bench.WatchedAssertion(
                        "9.9.9",
                        "HTTPX requires Python 3.7+",
                        "HTTPX requires Python 3.7 or later.",
                    ),
                    doc.watched[1],
                ),
            )
            for doc in bench.BENCHMARK_DOCS
        )
        with mock.patch.object(bench, "BENCHMARK_DOCS", tampered):
            with self.assertRaises(BenchmarkError) as caught:
                validate_plan(corpus)
        self.assertEqual("unknown_corpus_version", caught.exception.code)

    def test_equivalence_oracle_detects_divergence(self) -> None:
        current = {"claims": {"claim:x": "accepted"}, "conclusions": {}}
        recomputed = {"claims": {"claim:x": "unsupported"}, "conclusions": {}}
        with self.assertRaises(BenchmarkError) as caught:
            assert_equivalent(current, recomputed)
        self.assertEqual("equivalence_violation", caught.exception.code)
        # Matching maps pass silently.
        assert_equivalent(recomputed, recomputed)

    def test_unplanned_extraction_era_fails_fast(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        env_doc = next(
            doc for doc in bench.BENCHMARK_DOCS if doc.doc_id == "environment_variables"
        )
        # The plan never extracts environment_variables at the 0.26.0 era
        # (0.24.1 -> 0.27.2 is one real content transition), so an extraction
        # there means the plan and the corpus disagree.
        with self.assertRaises(BenchmarkError) as caught:
            bench._extract(corpus, env_doc, "2023-12-20T14:52:22+04:00", "2026-08-30T00:00:00Z")
        self.assertEqual("unplanned_extraction_era", caught.exception.code)

    def test_replayed_event_does_not_double_count(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        doc = next(item for item in bench.BENCHMARK_DOCS if item.doc_id == "index")
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "replay.sqlite3")
            try:
                bridge = GraphBridge(repository, corpus)
                bundle = bench._extract(corpus, doc, bench.T0_AS_OF, bench.T0_REASONED_AT)
                bridge.load_bundle(bundle, observed_at=bench.T0_REASONED_AT)
                bridge.record_initial_assessments(
                    snapshot_id="M1-5B:T0",
                    rule_version=bench.RULE_VERSION,
                    reasoned_at=bench.T0_REASONED_AT,
                )
                event, new_source = bridge.revision_event(
                    "index", "0.24.1", "0.25.2", project_id=bench.PROJECT_ID
                )
                bundle_t1 = bench._extract(
                    corpus, doc, "2023-11-24T16:33:18+04:00", "2023-11-24T16:33:18+04:00"
                )
                package = ChangePackage(
                    scenario_id=bench.SCENARIO_ID,
                    scenario_version=bench.SCENARIO_VERSION,
                    input_snapshot_id=f"{bench.SCENARIO_ID}:T0",
                    input_snapshot_hash=bench._snapshot_hash(repository),
                    rule_version=bench.RULE_VERSION,
                    event=event,
                    new_source=new_source,
                    new_claims=bundle_t1.claims,
                    new_evidence=bundle_t1.evidence_spans,
                    new_edges=bundle_t1.edges,
                )
                engine = EvolutionEngine(repository)
                first = engine.apply(package)
                counts = repository.entity_counts()
                replayed = engine.apply(package)
                self.assertEqual(first.run_id, replayed.run_id)
                self.assertEqual(counts, repository.entity_counts())
            finally:
                repository.close()

    def test_full_recompute_state_reports_stored_graph(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        doc = next(item for item in bench.BENCHMARK_DOCS if item.doc_id == "index")
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "oracle.sqlite3")
            try:
                bridge = GraphBridge(repository, corpus)
                bundle = bench._extract(corpus, doc, bench.T0_AS_OF, bench.T0_REASONED_AT)
                bridge.load_bundle(bundle, observed_at=bench.T0_REASONED_AT)
                bridge.record_initial_assessments(
                    snapshot_id="M1-5B:T0",
                    rule_version=bench.RULE_VERSION,
                    reasoned_at=bench.T0_REASONED_AT,
                )
                bridge.record_initial_conclusion(
                    conclusion_key="index_fact_supported",
                    claim_ids=tuple(claim.claim_id for claim in bundle.claims),
                    pass_statement="supported",
                    fail_statement="unsupported",
                    rule_version=bench.RULE_VERSION,
                    reasoned_at=bench.T0_REASONED_AT,
                )
                state = full_recompute_state(repository)
                self.assertEqual(
                    {"accepted"}, set(state["claims"].values())
                )
                self.assertEqual(
                    {"index_fact_supported": "pass"}, state["conclusions"]
                )
                # The oracle agrees with the stored current view.
                assert_equivalent(
                    {
                        "claims": {
                            claim.claim_id: repository.get_current_assessment(
                                claim.claim_id
                            ).assessment.value
                            for claim in repository.list_claims()
                        },
                        "conclusions": {
                            conclusion.conclusion_key: conclusion.outcome.value
                            for conclusion in repository.list_current_conclusions()
                        },
                    },
                    state,
                )
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
