from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.evaluation.extraction_runner import run_extraction_calibration


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2b" / "benchmark.json"
FIXTURES = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2b" / "fixtures.json"
CORPUS = PROJECT_ROOT / "datasets" / "corpus" / "httpx-docs"


class ExtractionCalibrationV2Tests(unittest.TestCase):
    def test_expanded_thirty_case_calibration_passes(self) -> None:
        summary = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS,
        )
        self.assertEqual("httpx-initial-extraction", summary["benchmark_id"])
        self.assertEqual("2.0.0", summary["benchmark_version"])
        self.assertEqual(30, summary["case_count"])
        self.assertEqual(30, summary["passed_case_count"])
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertEqual(0, summary["major_failure_count"])
        self.assertTrue(summary["m1_2a_acceptance_candidate"])
        self.assertEqual(1.0, summary["metrics"]["retrieval_hit_at_k"])
        self.assertEqual(1.0, summary["metrics"]["assertion_micro_precision"])
        self.assertEqual(1.0, summary["metrics"]["assertion_micro_recall"])
        self.assertEqual(1.0, summary["metrics"]["citation_exact_alignment"])
        self.assertLess(summary["metrics"]["mean_reciprocal_rank"], 1.0)
        content_hash = summary.pop("content_hash")
        canonical = json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), content_hash)

    def test_v2_is_superset_of_frozen_v1_cases(self) -> None:
        benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        v1 = json.loads(
            (PROJECT_ROOT / "datasets/extraction/httpx-m1-2a/benchmark.json").read_text(
                encoding="utf-8"
            )
        )
        v1_cases = {case["case_id"]: case for case in v1["cases"]}
        for case in benchmark["cases"][:10]:
            self.assertEqual(v1_cases[case["case_id"]], case)

    def test_expanded_coverage_shapes(self) -> None:
        benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        cases = {case["case_id"]: case for case in benchmark["cases"]}
        self.assertEqual(30, len(cases))
        multi = [case for case in cases.values() if len(case["expected_assertions"]) > 1]
        self.assertEqual({"EX-014", "EX-024"}, {case["case_id"] for case in multi})
        contradicts = {
            case["case_id"]
            for case in cases.values()
            if any(a["relation"] == "contradicts" for a in case["expected_assertions"])
        }
        self.assertEqual({"EX-017", "EX-018", "EX-019", "EX-022"}, contradicts)
        as_of_cases = {case["case_id"]: case for case in cases.values() if case.get("as_of")}
        self.assertEqual({"EX-029", "EX-030"}, set(as_of_cases))

    def test_as_of_case_versions_match_fixture_map(self) -> None:
        summary = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS,
        )
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
        fixture_cases = {case["case_id"]: case for case in fixtures["cases"]}
        for case in summary["cases"]:
            expected_versions = fixture_cases[case["case_id"]]["versions"]
            self.assertEqual(
                sorted(expected_versions),
                sorted(set(case["retrieved_doc_ids"])),
            )
        ex029 = next(case for case in summary["cases"] if case["case_id"] == "EX-029")
        self.assertIn("index", ex029["retrieved_doc_ids"])
        fixtures_index_version = fixture_cases["EX-029"]["versions"]["index"]
        self.assertEqual("0.24.1", fixtures_index_version)

    def test_calibration_is_deterministic(self) -> None:
        first = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS,
        )
        second = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS,
        )
        self.assertEqual(first, second)

    def test_fixture_question_drift_is_rejected(self) -> None:
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
        fixtures["cases"][11]["question"] = "Changed question"
        with tempfile.TemporaryDirectory() as temporary_directory:
            changed = Path(temporary_directory) / "fixtures.json"
            changed.write_text(json.dumps(fixtures), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "EX05_FIXTURE_DRIFT"):
                run_extraction_calibration(
                    benchmark_path=BENCHMARK,
                    fixtures_path=changed,
                    corpus_root=CORPUS,
                )


if __name__ == "__main__":
    unittest.main()
