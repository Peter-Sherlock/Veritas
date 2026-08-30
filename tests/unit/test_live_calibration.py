from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from veritas.evaluation.extraction_runner import (
    build_extraction_prompt,
    build_fixture_provider,
    run_live_extraction_calibration,
)
from veritas.extraction.pipeline import EXTRACTION_SYSTEM_PROMPT
from veritas.providers.llm import FixtureLLM, LLMResponse, fixture_key
from veritas.search.local_corpus import LocalCorpusProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2a" / "benchmark.json"
FIXTURES = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2a" / "fixtures.json"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"


class _ReplayLiveLLM:
    """Simulates a live provider by replaying the frozen fixture responses."""

    def __init__(self, fixtures_path: Path, response_override: str | None = None) -> None:
        self._inner = build_fixture_provider(
            json.loads(BENCHMARK.read_text(encoding="utf-8")),
            json.loads(fixtures_path.read_text(encoding="utf-8")),
            LocalCorpusProvider(CORPUS_ROOT),
        )
        self._override = response_override
        self.model_id = "live-replay-model"

    def complete(self, *, system: str, prompt: str, json_mode: bool = True):
        if self._override is not None:
            return LLMResponse(text=self._override, model_id=self.model_id)
        return self._inner.complete(system=system, prompt=prompt, json_mode=json_mode)


class LiveExtractionCalibrationTests(unittest.TestCase):
    def test_live_run_scores_and_records_replayable_responses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "recorded" / "responses.json"
            summary = run_live_extraction_calibration(
                benchmark_path=BENCHMARK,
                corpus_root=CORPUS_ROOT,
                model="live-replay-model",
                base_url="https://example.invalid",
                record_path=record_path,
                provider=_ReplayLiveLLM(FIXTURES),
            )
            self.assertTrue(record_path.exists())
            self.assertEqual("live-replay-model", summary["model_id"])
            self.assertEqual("live-recording:live-replay-model", summary["fixture_id"])
            self.assertEqual(10, summary["case_count"])
            self.assertEqual(10, summary["passed_case_count"])
            self.assertTrue(summary["m1_2a_acceptance_candidate"])

            # The recording must replay the exact live exchange afterwards.
            recorded = FixtureLLM.from_json(record_path)
            self.assertEqual("live-replay-model", recorded.model_id)
            corpus = LocalCorpusProvider(CORPUS_ROOT)
            benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
            case = benchmark["cases"][0]
            document = corpus.fetch(
                case["expected_retrieval"]["doc_id"],
                _fixture_version(benchmark, case),
            )
            key = fixture_key(
                EXTRACTION_SYSTEM_PROMPT,
                build_extraction_prompt(case["question"], document),
            )
            raw = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertIn(key, raw["responses"])

    def test_live_run_records_contract_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "recorded.json"
            summary = run_live_extraction_calibration(
                benchmark_path=BENCHMARK,
                corpus_root=CORPUS_ROOT,
                model="live-replay-model",
                base_url="https://example.invalid",
                record_path=record_path,
                provider=_ReplayLiveLLM(FIXTURES, response_override="{not json"),
            )
        self.assertEqual(0, summary["passed_case_count"])
        self.assertEqual(10, summary["failure_counts"]["EX02_CONTRACT_REJECTION"])
        self.assertEqual(10, summary["critical_failure_count"])
        self.assertEqual(0, summary["major_failure_count"])
        self.assertFalse(summary["m1_2a_acceptance_candidate"])

    def test_live_run_streams_progress_and_saves_recording_per_case(self) -> None:
        import contextlib
        import io

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "recorded.json"
            with contextlib.redirect_stderr(stderr):
                summary = run_live_extraction_calibration(
                    benchmark_path=BENCHMARK,
                    corpus_root=CORPUS_ROOT,
                    model="live-replay-model",
                    base_url="https://example.invalid",
                    record_path=record_path,
                    provider=_ReplayLiveLLM(FIXTURES),
                )
        lines = [line for line in stderr.getvalue().splitlines() if line.startswith("[live]")]
        self.assertEqual(10, len(lines))
        self.assertIn("EX-001 pass", lines[0])
        self.assertIn("10/10 EX-010", lines[-1])
        self.assertIn("live provider recording:", stderr.getvalue())
        # A finished run must still leave a complete, replayable recording.
        self.assertEqual(10, summary["passed_case_count"])

    def test_live_run_without_provider_requires_api_key(self) -> None:
        original = os.environ.pop("VERITAS_LLM_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    run_live_extraction_calibration(
                        benchmark_path=BENCHMARK,
                        corpus_root=CORPUS_ROOT,
                        model="deepseek-v4-flash",
                        base_url="https://api.deepseek.com",
                        record_path=Path(tmp) / "recorded.json",
                    )
        finally:
            if original is not None:
                os.environ["VERITAS_LLM_API_KEY"] = original


def _fixture_version(benchmark: dict, case: dict) -> str:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixture_case = {
        item["case_id"]: item for item in fixtures["cases"]
    }[case["case_id"]]
    return fixture_case["versions"][case["expected_retrieval"]["doc_id"]]


if __name__ == "__main__":
    unittest.main()
