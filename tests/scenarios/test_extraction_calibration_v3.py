from __future__ import annotations

import json
import unittest
from pathlib import Path

from veritas.evaluation.extraction_runner import build_fixture_provider, run_extraction_calibration
from veritas.extraction.pipeline import EXTRACTION_SCHEMA_VERSION
from veritas.providers.llm import FixtureLLM
from veritas.search.local_corpus import LocalCorpusProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2c" / "benchmark.json"
FIXTURES = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2c" / "fixtures.json"
V2_BENCHMARK = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2b" / "benchmark.json"
CORPUS_ROOT = PROJECT_ROOT / "datasets" / "corpus" / "httpx-docs"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ExtractionCalibrationV3Tests(unittest.TestCase):
    def test_full_benchmark_passes_with_frozen_metrics(self) -> None:
        summary = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS_ROOT,
        )
        self.assertEqual("3.0.0", summary["benchmark_version"])
        self.assertEqual(EXTRACTION_SCHEMA_VERSION, summary["schema_version"])
        self.assertEqual(30, summary["case_count"])
        self.assertEqual(30, summary["passed_case_count"])
        self.assertEqual(1.0, summary["metrics"]["retrieval_hit_at_k"])
        self.assertEqual(0.7222222222222221, summary["metrics"]["mean_reciprocal_rank"])
        self.assertEqual(1.0, summary["metrics"]["assertion_micro_precision"])
        self.assertEqual(1.0, summary["metrics"]["assertion_micro_recall"])
        self.assertEqual(1.0, summary["metrics"]["citation_exact_alignment"])
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertEqual(0, summary["major_failure_count"])
        self.assertTrue(summary["m1_2a_acceptance_candidate"])

    def test_v3_is_a_question_superset_of_frozen_v2(self) -> None:
        v3 = _load(BENCHMARK)
        v2 = _load(V2_BENCHMARK)
        self.assertEqual(
            [case["question"] for case in v2["cases"]],
            [case["question"] for case in v3["cases"]],
        )
        self.assertEqual(
            [case["case_id"] for case in v2["cases"]],
            [case["case_id"] for case in v3["cases"]],
        )

    def test_gold_assertions_carry_no_canonical_key(self) -> None:
        benchmark = _load(BENCHMARK)
        for case in benchmark["cases"]:
            for assertion in case["expected_assertions"]:
                self.assertEqual(
                    {"doc_id", "statement", "relation", "quote"},
                    set(assertion),
                )
        fixtures = _load(FIXTURES)
        for case in fixtures["cases"]:
            for response in case["responses"].values():
                for assertion in response["assertions"]:
                    self.assertEqual(
                        {"statement", "relation", "quote"},
                        set(assertion),
                    )

    def test_coverage_shapes_are_carried_over(self) -> None:
        benchmark = _load(BENCHMARK)
        by_id = {case["case_id"]: case for case in benchmark["cases"]}
        multi = {
            case_id
            for case_id, case in by_id.items()
            if len(case["expected_assertions"]) > 1
        }
        contradicts = {
            case_id
            for case_id, case in by_id.items()
            if any(a["relation"] == "contradicts" for a in case["expected_assertions"])
        }
        as_of = {case_id for case_id, case in by_id.items() if case.get("as_of")}
        self.assertEqual({"EX-014", "EX-024"}, multi)
        self.assertEqual({"EX-017", "EX-018", "EX-019", "EX-022"}, contradicts)
        self.assertEqual({"EX-029", "EX-030"}, as_of)

    def test_as_of_case_resolves_the_pinned_historical_version(self) -> None:
        fixtures = _load(FIXTURES)
        by_id = {case["case_id"]: case for case in fixtures["cases"]}
        self.assertEqual("0.24.1", by_id["EX-029"]["versions"]["index"])
        self.assertEqual("0.25.2", by_id["EX-030"]["versions"]["troubleshooting"])

    def test_pipeline_materializes_candidates_with_derived_keys(self) -> None:
        benchmark = _load(BENCHMARK)
        fixtures = _load(FIXTURES)
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        provider = build_fixture_provider(benchmark, fixtures, corpus)
        from veritas.extraction.pipeline import (
            ResearchExtractionPipeline,
            derive_canonical_key,
        )

        case = next(c for c in benchmark["cases"] if c["case_id"] == "EX-001")
        pipeline = ResearchExtractionPipeline(corpus, provider, source_namespace="httpx-docs")
        bundle = pipeline.run(
            query=case["query"],
            question=case["question"],
            reasoned_at=benchmark["reasoned_at"],
            top_k=case["top_k"],
            as_of=case.get("as_of"),
        )
        self.assertTrue(bundle.claims)
        self.assertTrue(bundle.evidence_spans)
        self.assertTrue(bundle.edges)
        for claim in bundle.claims:
            self.assertEqual(derive_canonical_key(claim.statement), claim.canonical_key)
            self.assertRegex(claim.canonical_key, r"^[a-z0-9][a-z0-9._:=/-]*$")
            self.assertNotIn(" ", claim.canonical_key)

    def test_calibration_is_deterministic_across_runs(self) -> None:
        first = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS_ROOT,
        )
        second = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS_ROOT,
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
