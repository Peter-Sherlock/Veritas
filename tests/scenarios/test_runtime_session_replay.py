from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from veritas.runtime.cli import main
from veritas.search.local_corpus import LocalCorpusProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_DIR = REPO_ROOT / "artifacts" / "runtime" / "httpx-session-m1-3b"
SPEC = SESSION_DIR / "session-spec.json"
RECORDING = SESSION_DIR / "responses-recording.json"
COMMITTED_SUMMARY = SESSION_DIR / "session-summary.json"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"

OBSERVED_AT = "2026-08-30T00:00:00Z"


class RuntimeSessionReplayTests(unittest.TestCase):
    def test_committed_live_session_replays_to_committed_summary(self) -> None:
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        recording = json.loads(RECORDING.read_text(encoding="utf-8"))
        committed = json.loads(COMMITTED_SUMMARY.read_text(encoding="utf-8"))

        self.assertEqual("deepseek-v4-flash", recording["model_id"])
        self.assertEqual("httpx-session-m1-3b", committed["session_id"])
        self.assertEqual("completed", committed["status"])
        self.assertEqual(7, committed["requests_spent"])
        self.assertEqual(2, committed["items_rejected"])
        # Pinned failure codes: the live model's citation violations were
        # blocked at the contract boundary and recorded as terminal rejections.
        codes = {
            item["item_id"]: item["last_error"]
            for item in committed["items"]
            if item["status"] == "rejected"
        }
        self.assertEqual(
            {"EX-001": "citation_ambiguous", "EX-017": "citation_not_found"}, codes
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary_path = tmp_path / "summary.json"
            code = main(
                [
                    "--spec", str(SPEC),
                    "--corpus-root", str(CORPUS_ROOT),
                    "--runtime-store", str(tmp_path / "runtime.db"),
                    "--candidates-out", str(tmp_path / "candidates.db"),
                    "--provider", "replay",
                    "--record-in", str(RECORDING),
                    "--observed-at", OBSERVED_AT,
                    "--output", str(summary_path),
                ]
            )
            self.assertEqual(0, code)
            replayed = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(committed, replayed)

            from veritas.extraction.store import CandidateStore

            with CandidateStore(tmp_path / "candidates.db") as candidates:
                # The one contract-passing item contributed exactly one
                # candidate, attributed to the session run id.
                self.assertEqual(
                    {"candidates": 1, "observations": 1, "distinct_canonical_keys": 1},
                    candidates.counts(),
                )
                run_ids = {
                    row["run_id"]
                    for row in candidates.connection.execute(
                        "SELECT run_id FROM extraction_candidate_observations"
                    )
                }
                self.assertEqual({"session:httpx-session-m1-3b"}, run_ids)

    def test_replayed_session_rerun_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary_path = tmp_path / "summary.json"
            argv = [
                "--spec", str(SPEC),
                "--corpus-root", str(CORPUS_ROOT),
                "--runtime-store", str(tmp_path / "runtime.db"),
                "--provider", "replay",
                "--record-in", str(RECORDING),
                "--observed-at", OBSERVED_AT,
                "--output", str(summary_path),
            ]
            self.assertEqual(0, main(argv))
            first = json.loads(summary_path.read_text(encoding="utf-8"))
            # A completed session rerun re-prints the same summary.
            self.assertEqual(0, main(argv))
            self.assertEqual(first, json.loads(summary_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
