from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_prompt,
)
from veritas.providers.llm import fixture_key
from veritas.runtime.cli import main
from veritas.search.local_corpus import LocalCorpusProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2c"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"
OBSERVED_AT = "2026-08-30T00:00:00Z"

# Two benchmark cases whose gold document ranks first, replayed at top_k 1.
CASE_IDS = ("EX-001", "EX-005")


def _spec_dict() -> dict:
    benchmark = json.loads((DATASET / "benchmark.json").read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in benchmark["cases"]}
    return {
        "session_id": "cli-demo",
        "budget_requests": 10,
        "items": [
            {
                "item_id": case_id,
                "query": cases[case_id]["query"],
                "question": cases[case_id]["question"],
                "top_k": 1,
            }
            for case_id in CASE_IDS
        ],
    }


def _recording_for(spec: dict, responses_path: Path) -> None:
    """Build a replay recording from the frozen fixture responses.

    Only the prompts the session actually hits are included: each item
    retrieves exactly its gold document at top_k 1.
    """
    corpus = LocalCorpusProvider(CORPUS_ROOT)
    fixtures = json.loads((DATASET / "fixtures.json").read_text(encoding="utf-8"))
    fixture_cases = {case["case_id"]: case for case in fixtures["cases"]}
    responses: dict[str, str] = {}
    for item in spec["items"]:
        fixture_case = fixture_cases[item["item_id"]]
        retrieved = corpus.search(item["query"], top_k=item["top_k"])
        for result in retrieved:
            document = corpus.fetch(result.doc_id, result.version_id)
            key = fixture_key(
                EXTRACTION_SYSTEM_PROMPT,
                build_extraction_prompt(item["question"], document),
            )
            responses[key] = json.dumps(
                fixture_case["responses"][result.doc_id], ensure_ascii=False
            )
    responses_path.write_text(
        json.dumps(
            {"model_id": fixtures["model_id"], "responses": responses},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _run_cli(argv: list[str]) -> tuple[int, str]:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stderr.getvalue()


class RuntimeCliTests(unittest.TestCase):
    def test_replay_session_completes_and_rerun_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec = _spec_dict()
            spec_path = tmp_path / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            recording = tmp_path / "recording.json"
            _recording_for(spec, recording)
            summary_path = tmp_path / "summary.json"

            argv = [
                "--spec", str(spec_path),
                "--corpus-root", str(CORPUS_ROOT),
                "--runtime-store", str(tmp_path / "runtime.db"),
                "--candidates-out", str(tmp_path / "candidates.db"),
                "--provider", "replay",
                "--record-in", str(recording),
                "--observed-at", OBSERVED_AT,
                "--output", str(summary_path),
            ]
            code, stderr = _run_cli(argv)
            self.assertEqual(0, code)
            self.assertIn("[session] 1/2 EX-001 completed requests=1", stderr)
            self.assertIn("[session] 2/2 EX-005 completed requests=2", stderr)
            self.assertIn("research session: status=completed", stderr)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", summary["status"])
            self.assertEqual(2, summary["items_completed"])
            self.assertEqual(2, summary["requests_spent"])
            self.assertEqual(
                {"candidates": 2, "observations": 2, "distinct_canonical_keys": 2},
                summary["candidate_store"],
            )
            canonical = json.dumps(
                {k: v for k, v in summary.items() if k != "content_hash"},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                hashlib.sha256(canonical).hexdigest(), summary["content_hash"]
            )

            # Rerunning the same command is safe: the completed session is
            # detected and its summary is reprinted without new work.
            code, stderr = _run_cli(argv)
            self.assertEqual(0, code)
            self.assertIn("already completed", stderr)
            self.assertEqual(summary, json.loads(summary_path.read_text(encoding="utf-8")))

    def test_spec_validation_failures_are_clean_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_json = tmp_path / "bad.json"
            bad_json.write_text("{not json", encoding="utf-8")
            empty_items = tmp_path / "empty.json"
            empty_items.write_text(
                json.dumps({"session_id": "s", "budget_requests": 5, "items": []}),
                encoding="utf-8",
            )
            bad_budget = tmp_path / "budget.json"
            bad_budget.write_text(
                json.dumps(
                    {
                        "session_id": "s",
                        "budget_requests": 0,
                        "items": [_spec_dict()["items"][0]],
                    }
                ),
                encoding="utf-8",
            )
            dup = tmp_path / "dup.json"
            spec = _spec_dict()
            spec["items"][1]["item_id"] = spec["items"][0]["item_id"]
            dup.write_text(json.dumps(spec), encoding="utf-8")

            argv_base = [
                "--corpus-root", str(CORPUS_ROOT),
                "--runtime-store", str(tmp_path / "runtime.db"),
            ]
            for spec_path in (
                "no-such-file.json",
                str(bad_json),
                str(empty_items),
                str(bad_budget),
                str(dup),
            ):
                with self.subTest(spec=spec_path):
                    with self.assertRaises(SystemExit) as caught:
                        main(["--spec", spec_path] + argv_base)
                    self.assertEqual(2, caught.exception.code)

    def test_live_without_api_key_is_a_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec_path = tmp_path / "spec.json"
            spec_path.write_text(json.dumps(_spec_dict()), encoding="utf-8")
            original = os.environ.pop("VERITAS_LLM_API_KEY", None)
            try:
                with self.assertRaises(SystemExit) as caught:
                    main(
                        [
                            "--spec", str(spec_path),
                            "--corpus-root", str(CORPUS_ROOT),
                            "--runtime-store", str(tmp_path / "runtime.db"),
                            "--provider", "live",
                        ]
                    )
                self.assertEqual(2, caught.exception.code)
            finally:
                if original is not None:
                    os.environ["VERITAS_LLM_API_KEY"] = original

    def test_provider_flag_combinations_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec_path = tmp_path / "spec.json"
            spec_path.write_text(json.dumps(_spec_dict()), encoding="utf-8")
            argv_base = [
                "--spec", str(spec_path),
                "--corpus-root", str(CORPUS_ROOT),
                "--runtime-store", str(tmp_path / "runtime.db"),
            ]
            with self.assertRaises(SystemExit) as caught:
                main(argv_base + ["--provider", "replay"])
            self.assertEqual(2, caught.exception.code)
            with self.assertRaises(SystemExit) as caught:
                main(
                    argv_base
                    + [
                        "--provider", "replay",
                        "--record-in", "r.json",
                        "--record-out", "o.json",
                    ]
                )
            self.assertEqual(2, caught.exception.code)

    def test_budget_decrease_on_resume_is_a_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec = _spec_dict()
            spec["budget_requests"] = 10
            spec_path = tmp_path / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            recording = tmp_path / "recording.json"
            _recording_for(spec, recording)
            argv_base = [
                "--spec", str(spec_path),
                "--corpus-root", str(CORPUS_ROOT),
                "--runtime-store", str(tmp_path / "runtime.db"),
                "--provider", "replay",
                "--record-in", str(recording),
                "--observed-at", OBSERVED_AT,
            ]
            code, _ = _run_cli(argv_base)
            self.assertEqual(0, code)

            spec["budget_requests"] = 5
            smaller = tmp_path / "spec-smaller.json"
            smaller.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                main(["--spec", str(smaller)] + argv_base[1:])
            self.assertEqual(2, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
