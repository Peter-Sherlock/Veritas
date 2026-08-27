from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from veritas.domain.enums import Assessment
from veritas.evaluation.runner import run_scenario
from veritas.storage.sqlite import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-002" / "scenario.json"


class RetractScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database_path = root / "veritas.sqlite3"
        self.result = run_scenario(SCENARIO_PATH, self.database_path, root / "artifacts")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_retract_rechecks_redundant_claim_without_semantic_change(self) -> None:
        run = self.result.run
        self.assertEqual(("EV_GUIDE_RETRY@1",), run.candidate_impact.evidence_spans)
        self.assertEqual(("retry_supported",), run.candidate_impact.claims)
        self.assertEqual(("retry_policy_fit",), run.candidate_impact.conclusions)
        self.assertEqual((), run.confirmed_invalidations)
        self.assertEqual(("retry_supported",), run.rechecked_unchanged)
        self.assertEqual((), run.recomputed_conclusions)
        self.assertEqual((), run.created_conclusions)

    def test_current_assessment_excludes_retracted_evidence(self) -> None:
        with SQLiteRepository(self.database_path) as repository:
            assessment = repository.get_current_assessment("retry_supported")
            active_edges = repository.list_active_evidence_edges_for_claim("retry_supported")
        self.assertEqual(Assessment.ACCEPTED, assessment.assessment)
        self.assertEqual(("EDGE_API_RETRY_SUPPORT@1",), assessment.reason_refs)
        self.assertEqual(["EDGE_API_RETRY_SUPPORT@1"], [edge.edge_id for edge in active_edges])

    def test_retract_is_append_only_and_does_not_version_conclusions(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            old_source_valid_to = connection.execute(
                "SELECT valid_to FROM source_versions WHERE version_id = 'SRC_RETRY_GUIDE@1.0'"
            ).fetchone()[0]
            retry_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'retry_policy_fit'"
            ).fetchone()[0]
            python_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'python_311_compatible'"
            ).fetchone()[0]
        self.assertIsNone(old_source_valid_to)
        self.assertEqual(1, retry_versions)
        self.assertEqual(1, python_versions)
        self.assertIn(
            "source_version_retracted",
            {event["event_type"] for event in self.result.run.trace_events},
        )

    def test_zero_diff_artifact_and_metrics_are_explicit(self) -> None:
        diff_payload = json.loads(
            (self.result.artifact_dir / "conclusion_diff.json").read_text(encoding="utf-8")
        )
        self.assertEqual([], diff_payload["conclusion_diffs"])
        metrics = self.result.metrics
        self.assertEqual(0.0, metrics["selective_recompute_ratio"])
        self.assertTrue(metrics["full_recompute_equivalent"])
        self.assertTrue(metrics["provenance_integrity"])
        self.assertEqual(0, metrics["critical_failure_count"])
        self.assertEqual([], metrics["failures"])


if __name__ == "__main__":
    unittest.main()
