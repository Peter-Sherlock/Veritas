from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from veritas.evaluation.extraction_runner import (
    EX_ASSERTION_MISMATCH,
    EX_CITATION_REJECTION,
    EX_CONTRACT_REJECTION,
    EX_FAILURE_CODES,
    EX_FIXTURE_DRIFT,
    EX_RETRIEVAL_MISS,
    EXTRACTION_FAILURE_TAXONOMY,
    build_fixture_provider,
    evaluate_extraction_calibration,
    run_extraction_calibration,
)
from veritas.providers.llm import FixtureLLM, LLMResponse, fixture_key
from veritas.search.local_corpus import LocalCorpusProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2c" / "benchmark.json"
FIXTURES = PROJECT_ROOT / "datasets" / "extraction" / "httpx-m1-2c" / "fixtures.json"
CORPUS = PROJECT_ROOT / "datasets" / "corpus" / "httpx-docs"


def _load_benchmark() -> dict[str, Any]:
    return json.loads(BENCHMARK.read_text(encoding="utf-8"))


def _load_fixtures() -> dict[str, Any]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _evaluate(benchmark: dict[str, Any], fixtures: dict[str, Any], provider: Any) -> dict[str, Any]:
    return evaluate_extraction_calibration(
        benchmark=benchmark,
        fixtures=fixtures,
        corpus=LocalCorpusProvider(CORPUS),
        provider=provider,
    )


EX003_QUESTION_FRAGMENT = "async hot loop"


class _TargetedResponseLLM:
    """Delegates to the frozen fixture provider, replacing one case's responses.

    Targeting is by case-unique question fragment: top-3 documents overlap
    across the 30 cases, so a document id cannot isolate a single case.
    """

    def __init__(self, inner: FixtureLLM, target_fragment: str, response_text: str) -> None:
        self._inner = inner
        self._target_fragment = target_fragment
        self._response_text = response_text

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    def complete(self, *, system: str, prompt: str, json_mode: bool = True) -> LLMResponse:
        if self._target_fragment in prompt:
            return LLMResponse(text=self._response_text, model_id=self._inner.model_id)
        return self._inner.complete(system=system, prompt=prompt, json_mode=json_mode)


class ExtractionFailureTaxonomyTests(unittest.TestCase):
    def test_normal_set_has_zero_failures_and_complete_counts(self) -> None:
        summary = run_extraction_calibration(
            benchmark_path=BENCHMARK,
            fixtures_path=FIXTURES,
            corpus_root=CORPUS,
        )
        self.assertEqual(EXTRACTION_FAILURE_TAXONOMY, summary["failure_taxonomy"])
        self.assertEqual(
            {code: 0 for code in EX_FAILURE_CODES},
            summary["failure_counts"],
        )
        self.assertEqual([], summary["failures"])
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertEqual(0, summary["major_failure_count"])
        self.assertTrue(summary["m1_2a_acceptance_candidate"])
        for case in summary["cases"]:
            self.assertEqual([], case["failures"])
            self.assertNotIn("failure", case)
        content_hash = summary.pop("content_hash")
        canonical = json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), content_hash)

    def test_ex01_retrieval_miss_is_major_and_independently_triggered(self) -> None:
        benchmark = _load_benchmark()
        fixtures = _load_fixtures()
        target = next(case for case in benchmark["cases"] if case["case_id"] == "EX-009")
        self.assertEqual(3, target["expected_retrieval"]["max_rank"])
        target["expected_retrieval"]["max_rank"] = 1
        provider = build_fixture_provider(benchmark, fixtures, LocalCorpusProvider(CORPUS))
        summary = _evaluate(benchmark, fixtures, provider)

        case = next(case for case in summary["cases"] if case["case_id"] == "EX-009")
        self.assertFalse(case["retrieval_pass"])
        self.assertEqual(3, case["retrieval_rank"])
        self.assertTrue(case["exact_match"])
        self.assertEqual(
            [EX_RETRIEVAL_MISS],
            [record["failure_code"] for record in case["failures"]],
        )
        record = case["failures"][0]
        self.assertEqual("major", record["severity"])
        self.assertEqual(["EX-009"], record["entity_refs"])
        self.assertEqual("retrieved at rank 3", record["actual"])
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertEqual(1, summary["major_failure_count"])
        self.assertFalse(summary["m1_2a_acceptance_candidate"])
        self.assertEqual(29, summary["passed_case_count"])

    def test_ex02_contract_rejection_is_critical_and_independently_triggered(self) -> None:
        benchmark = _load_benchmark()
        fixtures = _load_fixtures()
        corpus = LocalCorpusProvider(CORPUS)
        provider = build_fixture_provider(benchmark, fixtures, corpus)
        broken = _TargetedResponseLLM(provider, EX003_QUESTION_FRAGMENT, "{not json")
        summary = _evaluate(benchmark, fixtures, broken)

        case = next(case for case in summary["cases"] if case["case_id"] == "EX-003")
        self.assertEqual(1, len(case["failures"]))
        record = case["failures"][0]
        self.assertEqual(EX_CONTRACT_REJECTION, record["failure_code"])
        self.assertEqual("critical", record["severity"])
        self.assertEqual("invalid_json", record["reason"]["pipeline_code"])
        self.assertEqual(0, case["actual_assertion_count"])
        self.assertEqual(1, summary["critical_failure_count"])
        self.assertEqual(0, summary["major_failure_count"])
        self.assertFalse(summary["m1_2a_acceptance_candidate"])

    def test_ex02_rejects_legacy_schema_with_model_proposed_key(self) -> None:
        benchmark = _load_benchmark()
        fixtures = _load_fixtures()
        corpus = LocalCorpusProvider(CORPUS)
        provider = build_fixture_provider(benchmark, fixtures, corpus)
        legacy = json.dumps(
            {
                "assertions": [
                    {
                        "statement": "A legacy assertion",
                        "canonical_key": "test.legacy.key",
                        "relation": "supports",
                        "quote": "no such substring",
                    }
                ]
            }
        )
        broken = _TargetedResponseLLM(provider, EX003_QUESTION_FRAGMENT, legacy)
        summary = _evaluate(benchmark, fixtures, broken)

        case = next(case for case in summary["cases"] if case["case_id"] == "EX-003")
        record = case["failures"][0]
        self.assertEqual(EX_CONTRACT_REJECTION, record["failure_code"])
        self.assertEqual("invalid_schema", record["reason"]["pipeline_code"])
        self.assertEqual(1, summary["critical_failure_count"])
        self.assertEqual(0, summary["major_failure_count"])
        self.assertFalse(summary["m1_2a_acceptance_candidate"])

    def test_ex02_rejects_statement_without_alphanumeric_content(self) -> None:
        benchmark = _load_benchmark()
        fixtures = _load_fixtures()
        corpus = LocalCorpusProvider(CORPUS)
        provider = build_fixture_provider(benchmark, fixtures, corpus)
        degenerate = json.dumps(
            {
                "assertions": [
                    {"statement": "!!!", "relation": "supports", "quote": "ignored"}
                ]
            }
        )
        broken = _TargetedResponseLLM(provider, EX003_QUESTION_FRAGMENT, degenerate)
        summary = _evaluate(benchmark, fixtures, broken)

        case = next(case for case in summary["cases"] if case["case_id"] == "EX-003")
        record = case["failures"][0]
        self.assertEqual(EX_CONTRACT_REJECTION, record["failure_code"])
        self.assertEqual("critical", record["severity"])
        self.assertEqual("invalid_statement", record["reason"]["pipeline_code"])

    def test_ex03_citation_rejection_is_major_and_independently_triggered(self) -> None:
        benchmark = _load_benchmark()
        fixtures = _load_fixtures()
        corpus = LocalCorpusProvider(CORPUS)
        provider = build_fixture_provider(benchmark, fixtures, corpus)
        ungrounded = json.dumps(
            {
                "assertions": [
                    {
                        "statement": "Ungrounded claim",
                        "relation": "supports",
                        "quote": "no such substring exists in this document",
                    }
                ]
            }
        )
        broken = _TargetedResponseLLM(provider, EX003_QUESTION_FRAGMENT, ungrounded)
        summary = _evaluate(benchmark, fixtures, broken)

        case = next(case for case in summary["cases"] if case["case_id"] == "EX-003")
        self.assertEqual(1, len(case["failures"]))
        record = case["failures"][0]
        self.assertEqual(EX_CITATION_REJECTION, record["failure_code"])
        self.assertEqual("major", record["severity"])
        self.assertEqual("citation_not_found", record["reason"]["pipeline_code"])
        self.assertEqual(29 / 30, summary["metrics"]["citation_exact_alignment"])
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertEqual(1, summary["major_failure_count"])

    def test_ex04_assertion_mismatch_is_major_and_independently_triggered(self) -> None:
        benchmark = _load_benchmark()
        fixtures = _load_fixtures()
        benchmark["cases"][0]["expected_assertions"][0]["statement"] = "Wrong statement"
        corpus = LocalCorpusProvider(CORPUS)
        provider = build_fixture_provider(benchmark, fixtures, corpus)
        summary = _evaluate(benchmark, fixtures, provider)

        case = next(case for case in summary["cases"] if case["case_id"] == "EX-001")
        self.assertEqual(
            [EX_ASSERTION_MISMATCH],
            [record["failure_code"] for record in case["failures"]],
        )
        record = case["failures"][0]
        self.assertEqual("major", record["severity"])
        self.assertEqual(
            ["Wrong statement"], record["reason"]["missing_statements"]
        )
        self.assertEqual(1, len(record["reason"]["unexpected_statements"]))
        self.assertEqual(0, summary["critical_failure_count"])
        self.assertEqual(1, summary["major_failure_count"])
        self.assertFalse(summary["m1_2a_acceptance_candidate"])

    def test_ex04_ignores_case_and_punctuation_differences(self) -> None:
        benchmark = _load_benchmark()
        gold_statement = benchmark["cases"][0]["expected_assertions"][0]["statement"]
        benchmark["cases"][0]["expected_assertions"][0]["statement"] = (
            gold_statement.lower().rstrip(".") + "."
        )
        provider = build_fixture_provider(benchmark, _load_fixtures(), LocalCorpusProvider(CORPUS))
        summary = _evaluate(benchmark, _load_fixtures(), provider)

        case = next(case for case in summary["cases"] if case["case_id"] == "EX-001")
        self.assertEqual([], case["failures"])
        self.assertTrue(case["exact_match"])
        self.assertTrue(summary["m1_2a_acceptance_candidate"])

    def test_ex05_fixture_drift_is_rejected_with_taxonomy_code(self) -> None:
        benchmark = _load_benchmark()
        fixtures = _load_fixtures()
        fixtures["cases"][0]["versions"]["bogus-doc"] = "0.28.1"
        with self.assertRaisesRegex(ValueError, "EX05_FIXTURE_DRIFT"):
            build_fixture_provider(benchmark, fixtures, LocalCorpusProvider(CORPUS))

        canary_drift = _load_fixtures()
        canary_drift["prompt_canary"]["fixture_key"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "EX05_FIXTURE_DRIFT"):
            build_fixture_provider(benchmark, canary_drift, LocalCorpusProvider(CORPUS))

        wrong_prompt = _load_benchmark()
        wrong_prompt["prompt_version"] = "httpx-extractor-1"
        with self.assertRaisesRegex(ValueError, "EX05_FIXTURE_DRIFT"):
            evaluate_extraction_calibration(
                benchmark=wrong_prompt,
                fixtures=_load_fixtures(),
                corpus=LocalCorpusProvider(CORPUS),
                provider=build_fixture_provider(
                    wrong_prompt, _load_fixtures(), LocalCorpusProvider(CORPUS)
                ),
            )


if __name__ == "__main__":
    unittest.main()
