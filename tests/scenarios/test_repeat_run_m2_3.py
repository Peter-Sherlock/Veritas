from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from veritas.evaluation.aggregation_calibration import write_summary as _write
from veritas.evaluation.extraction_runner import evaluate_extraction_calibration
from veritas.evaluation.run_variance import run_variance
from veritas.providers.llm import FixtureLLM
from veritas.search.local_corpus import LocalCorpusProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2c" / "benchmark.json"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"
RUN1_DIR = REPO_ROOT / "artifacts" / "extraction" / "httpx-initial-extraction-3.0.0-deepseek-v4-flash"
RUN2_DIR = REPO_ROOT / "artifacts" / "extraction" / "httpx-initial-extraction-3.0.0-deepseek-v4-flash-repeat1"
COMMITTED_VARIANCE = REPO_ROOT / "artifacts" / "extraction" / "m2-3-run-variance" / "summary.json"

MODEL_ID = "deepseek-v4-flash"


class RepeatRunM23Tests(unittest.TestCase):
    """Frozen M2-3 evidence: a second live run under the same contract
    (D-045) and the run-to-run variance against the first (C3-R)."""

    def test_repeat_recording_replays_to_committed_summary(self) -> None:
        recording = json.loads((RUN2_DIR / "responses-recording.json").read_text(encoding="utf-8"))
        committed = json.loads((RUN2_DIR / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(MODEL_ID, recording["model_id"])
        replayed = evaluate_extraction_calibration(
            benchmark=json.loads(BENCHMARK.read_text(encoding="utf-8")),
            fixtures={"fixture_id": f"live-recording:{MODEL_ID}", "model_id": MODEL_ID},
            corpus=LocalCorpusProvider(CORPUS_ROOT),
            provider=FixtureLLM(recording["responses"], model_id=recording["model_id"]),
        )
        self.assertEqual(committed, replayed)
        # The repeat run's failure distribution is itself pinned.
        self.assertEqual(0, committed["critical_failure_count"])
        self.assertEqual(3, committed["failure_counts"]["EX03_CITATION_REJECTION"])
        self.assertEqual(27, committed["failure_counts"]["EX04_ASSERTION_MISMATCH"])

    def test_committed_variance_artifact_is_reproducible(self) -> None:
        summary = run_variance(
            benchmark_path=BENCHMARK,
            recording_a_path=RUN1_DIR / "responses-recording.json",
            recording_b_path=RUN2_DIR / "responses-recording.json",
            corpus_root=CORPUS_ROOT,
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "summary.json"
            _write(summary, output)
            self.assertEqual(COMMITTED_VARIANCE.read_bytes(), output.read_bytes())

    def test_variance_numbers_are_pinned(self) -> None:
        summary = run_variance(
            benchmark_path=BENCHMARK,
            recording_a_path=RUN1_DIR / "responses-recording.json",
            recording_b_path=RUN2_DIR / "responses-recording.json",
            corpus_root=CORPUS_ROOT,
        )
        # Run profiles: run1 52 candidates / 4 rejections, run2 66 / 3.
        self.assertEqual(
            {"candidates": 52, "distinct_keys": 52, "rejected_cases": 4},
            summary["runs"]["run1"],
        )
        self.assertEqual(
            {"candidates": 66, "distinct_keys": 66, "rejected_cases": 3},
            summary["runs"]["run2"],
        )
        # The C3-R headline: under the SAME contract at temperature 0 the
        # model repeats less than half of its own assertions at key level.
        self.assertEqual(37, summary["key_level"]["shared_keys"])
        self.assertAlmostEqual(37 / 81, summary["key_level"]["jaccard"])
        self.assertLess(summary["key_level"]["jaccard"], 0.5)
        # Only 8 of 30 cases produce identical assertion sets.
        self.assertEqual(
            {"identical": 8, "a_only": 0, "b_only": 0, "both_differ": 22},
            summary["case_level"],
        )
        # Rejections are mostly stable across runs.
        self.assertEqual(
            ["EX-001", "EX-002", "EX-025"], summary["rejected_cases"]["shared_rejections"]
        )


if __name__ == "__main__":
    unittest.main()
