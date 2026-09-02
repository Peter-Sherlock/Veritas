"""M3-C frozen evidence: a real DeepSeek model drove the autonomous loop (D-046).

Two live invocations over the real HTTPX corpus, committed as evidence in
``artifacts/autonomy/live-watch-demo/``:

* **pass 1** (``watch-report-1.json``): T0 bootstrap with two questions —
  the model's DEMO-2 response was contract-rejected (``citation_not_found``)
  and contributed nothing — then three real corpus drifts, a re-research
  session, and an honest *non-repair*: the watched Python floor (3.7+)
  is genuinely gone from the corpus, so the conclusion stays unknown.
* **pass 2** (``watch-report-2.json``): a surviving fact (redirect
  behaviour) bootstrapped and drifted; re-research re-asserted the same
  fact from the new version and the refresh repaired the conclusion
  ``pass@1 -> unknown@2 -> pass@3``.

Both invocations are fully replayable: the committed recordings cover
every prompt and the reports are byte-deterministic given the pinned
``observed_at`` timestamps. The replay test re-runs both passes from the
committed evidence and byte-compares the reports.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from veritas.autonomy.cli import main as cli_main


REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "artifacts" / "autonomy" / "live-watch-demo"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"


def _observed_at(report_name: str) -> str:
    report = json.loads((DEMO / report_name).read_text(encoding="utf-8"))
    return str(report["observed_at"])


def _replay(root: Path, *, spec: str, recording: str, session_id: str, observed_at: str, report_name: str) -> bytes:
    output = root / report_name
    exit_code = cli_main(
        [
            "--corpus-root", str(CORPUS_ROOT),
            "--evolution-store", str(root / "evolution.db"),
            "--runtime-store", str(root / "runtime.db"),
            "--cluster-store", str(root / "clusters.db"),
            "--candidates-out", str(root / "candidates.db"),
            "--provider", "replay",
            "--record-in", str(DEMO / recording),
            "--init-spec", str(DEMO / spec),
            "--session-id", session_id,
            "--observed-at", observed_at,
            "--project-id", "live-watch-demo",
            "--output", str(output),
        ]
    )
    assert exit_code == 0, f"replay of {report_name} failed"
    return output.read_bytes()


class LiveWatchDemoM3CTests(unittest.TestCase):
    def test_replay_reproduces_both_committed_reports(self) -> None:
        # The live passes ran against ONE accumulating store: pass 2 saw
        # pass 1's graph state. The replay follows the same order on the
        # same store paths.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _replay(
                root,
                spec="t0-spec.json",
                recording="live-recording.json",
                session_id="live-watch-1",
                observed_at=_observed_at("watch-report-1.json"),
                report_name="watch-report-1.json",
            )
            self.assertEqual((DEMO / "watch-report-1.json").read_bytes(), first)
            second = _replay(
                root,
                spec="t0-spec-2.json",
                recording="live-recording-2.json",
                session_id="live-watch-2",
                observed_at=_observed_at("watch-report-2.json"),
                report_name="watch-report-2.json",
            )
            self.assertEqual((DEMO / "watch-report-2.json").read_bytes(), second)

    def test_pinned_story_fields(self) -> None:
        report1 = json.loads((DEMO / "watch-report-1.json").read_text(encoding="utf-8"))
        # The live model's DEMO-2 response was contract-rejected.
        demo2 = next(item for item in report1["t0"]["items"] if item["item_id"] == "DEMO-2")
        self.assertEqual("citation_not_found", demo2["rejected"])
        # Three real corpus drifts applied; the fact-changed branch leaves
        # the floor conclusion honestly unknown (re-research found nothing).
        self.assertEqual(
            ["CHG_ADVANCED_0.24.1_TO_0.26.0", "CHG_ASYNC_0.24.1_TO_0.28.1", "CHG_INDEX_0.24.1_TO_0.28.1"],
            [event["change_event_id"] for event in report1["drift_applied"]],
        )
        self.assertEqual({"t0_demo_1": "unknown"}, report1["final_conclusion_outcomes"])

        report2 = json.loads((DEMO / "watch-report-2.json").read_text(encoding="utf-8"))
        # The surviving fact was bootstrapped, drifted, and repaired.
        self.assertEqual(
            {"t0_demo_1": "unknown", "t0_demo_3": "pass"},
            report2["final_conclusion_outcomes"],
        )
        repair = report2["refreshes"][0]
        self.assertEqual(["t0_demo_3"], repair["recomputed_conclusions"])
        self.assertEqual(["t0_demo_3@3"], repair["created_conclusions"])
        # The re-research session spent a real, budgeted request set.
        self.assertEqual("completed", report2["session"]["status"])
        self.assertEqual(6, report2["session"]["requests_spent"])
        self.assertEqual(2, report2["session"]["items_completed"])


if __name__ == "__main__":
    unittest.main()
