from __future__ import annotations

import json
import unittest
from pathlib import Path

from veritas.evaluation.extraction_runner import evaluate_extraction_calibration
from veritas.providers.llm import FixtureLLM
from veritas.search.local_corpus import LocalCorpusProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2c" / "benchmark.json"
LIVE_DIR = REPO_ROOT / "artifacts" / "extraction" / "httpx-initial-extraction-3.0.0-deepseek-v4-flash"
RECORDING = LIVE_DIR / "responses-recording.json"
LIVE_SUMMARY = LIVE_DIR / "summary.json"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"

MODEL_ID = "deepseek-v4-flash"


class LiveRecordingReplayTests(unittest.TestCase):
    def test_recording_replays_to_committed_live_summary(self) -> None:
        recording = json.loads(RECORDING.read_text(encoding="utf-8"))
        committed = json.loads(LIVE_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(MODEL_ID, recording["model_id"])

        provider = FixtureLLM(recording["responses"], model_id=recording["model_id"])
        replayed = evaluate_extraction_calibration(
            benchmark=json.loads(BENCHMARK.read_text(encoding="utf-8")),
            fixtures={"fixture_id": f"live-recording:{MODEL_ID}", "model_id": MODEL_ID},
            corpus=LocalCorpusProvider(CORPUS_ROOT),
            provider=provider,
        )
        self.assertEqual(committed, replayed)

    def test_live_failure_distribution_is_pinned(self) -> None:
        committed = json.loads(LIVE_SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(30, committed["case_count"])
        self.assertEqual(0, committed["passed_case_count"])
        self.assertEqual(0, committed["failure_counts"]["EX01_RETRIEVAL_MISS"])
        self.assertEqual(0, committed["failure_counts"]["EX02_CONTRACT_REJECTION"])
        self.assertEqual(4, committed["failure_counts"]["EX03_CITATION_REJECTION"])
        self.assertEqual(26, committed["failure_counts"]["EX04_ASSERTION_MISMATCH"])
        self.assertEqual(0, committed["failure_counts"]["EX05_FIXTURE_DRIFT"])
        self.assertEqual(0, committed["critical_failure_count"])
        self.assertEqual(30, committed["major_failure_count"])
        self.assertEqual(1.0, committed["metrics"]["retrieval_hit_at_k"])
        self.assertEqual(26 / 30, committed["metrics"]["citation_exact_alignment"])
        self.assertFalse(committed["m1_2a_acceptance_candidate"])


if __name__ == "__main__":
    unittest.main()
