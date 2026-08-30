from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.evaluation.extraction_runner import run_extraction_calibration


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2a" / "benchmark.json"
FIXTURES = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2a" / "fixtures.json"
CORPUS = PROJECT_ROOT / "datasets" / "corpus" / "httpx-docs"


class ExtractionCalibrationTests(unittest.TestCase):
    def test_frozen_ten_case_calibration_passes(self) -> None:
        summary = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS,
        )
        self.assertEqual(10, summary["case_count"])
        self.assertEqual(10, summary["passed_case_count"])
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertTrue(summary["m1_2a_acceptance_candidate"])
        self.assertEqual(1.0, summary["metrics"]["retrieval_hit_at_k"])
        self.assertEqual(1.0, summary["metrics"]["assertion_micro_precision"])
        self.assertEqual(1.0, summary["metrics"]["assertion_micro_recall"])
        self.assertEqual(1.0, summary["metrics"]["citation_exact_alignment"])
        content_hash = summary.pop("content_hash")
        canonical = json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), content_hash)

    def test_calibration_is_deterministic_and_preserves_non_top1_ranks(self) -> None:
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
        ranks = {case["case_id"]: case["retrieval_rank"] for case in first["cases"]}
        self.assertEqual(2, ranks["EX-004"])
        self.assertEqual(3, ranks["EX-009"])
        self.assertLess(first["metrics"]["mean_reciprocal_rank"], 1.0)

    def test_statement_mismatch_is_recorded_as_ex04_major(self) -> None:
        benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        benchmark["cases"][0]["expected_assertions"][0]["statement"] = "Wrong statement"
        with tempfile.TemporaryDirectory() as temporary_directory:
            changed = Path(temporary_directory) / "benchmark.json"
            changed.write_text(json.dumps(benchmark), encoding="utf-8")
            summary = run_extraction_calibration(
                benchmark_path=changed,
                fixtures_path=FIXTURES,
                corpus_root=CORPUS,
            )
        first_case = summary["cases"][0]
        self.assertEqual("fail", first_case["status"])
        self.assertFalse(first_case["exact_match"])
        self.assertEqual(
            ["EX04_ASSERTION_MISMATCH"],
            [record["failure_code"] for record in first_case["failures"]],
        )
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertEqual(1, summary["major_failure_count"])
        self.assertFalse(summary["m1_2a_acceptance_candidate"])

    def test_fixture_question_drift_is_rejected(self) -> None:
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
        fixtures["cases"][0]["question"] = "Changed question"
        with tempfile.TemporaryDirectory() as temporary_directory:
            changed = Path(temporary_directory) / "fixtures.json"
            changed.write_text(json.dumps(fixtures), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixture question drift"):
                run_extraction_calibration(
                    benchmark_path=BENCHMARK,
                    fixtures_path=changed,
                    corpus_root=CORPUS,
                )

    def test_fixture_prompt_canary_drift_is_rejected(self) -> None:
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
        fixtures["prompt_canary"]["fixture_key"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary_directory:
            changed = Path(temporary_directory) / "fixtures.json"
            changed.write_text(json.dumps(fixtures), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixture prompt canary drift"):
                run_extraction_calibration(
                    benchmark_path=BENCHMARK,
                    fixtures_path=changed,
                    corpus_root=CORPUS,
                )


if __name__ == "__main__":
    unittest.main()
