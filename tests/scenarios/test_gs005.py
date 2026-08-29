from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from veritas.domain.enums import Assessment, ConclusionOutcome
from veritas.evaluation.runner import run_scenario
from veritas.storage.sqlite import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-005" / "scenario.json"


class ConflictScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database_path = root / "veritas.sqlite3"
        self.result = run_scenario(SCENARIO_PATH, self.database_path, root / "artifacts")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_only_contested_python_312_branch_is_invalidated(self) -> None:
        run = self.result.run
        candidate = set(run.candidate_impact.claims) | set(run.candidate_impact.conclusions)
        invalidated = {item["node_key"] for item in run.confirmed_invalidations}
        self.assertEqual(("EV_ADVISORY_PY312@1",), run.candidate_impact.evidence_spans)
        self.assertEqual({"python_312_supported", "python_312_compatible"}, candidate)
        self.assertEqual({"python_312_supported", "python_312_compatible"}, invalidated)
        self.assertEqual(
            {
                "retry_supported",
                "default_retries_3",
                "policy_min_retries_3",
                "retry_policy_fit",
            },
            set(run.untouched_nodes),
        )

    def test_conflict_keeps_both_sources_active_without_arbitration(self) -> None:
        with SQLiteRepository(self.database_path) as repository:
            py312 = repository.get_current_assessment("python_312_supported")
            compatibility = repository.get_current_conclusion("python_312_compatible")
            retry = repository.get_current_conclusion("retry_policy_fit")
            active_edges = repository.list_active_evidence_edges_for_claim("python_312_supported")
        self.assertEqual(Assessment.CONFLICT, py312.assessment)
        self.assertEqual(
            ("EDGE_RUNTIME_PY312_SUPPORT@1", "EDGE_ADVISORY_PY312_CONTRADICT@1"),
            py312.reason_refs,
        )
        self.assertEqual(
            ["EDGE_ADVISORY_PY312_CONTRADICT@1", "EDGE_RUNTIME_PY312_SUPPORT@1"],
            [edge.edge_id for edge in active_edges],
        )
        self.assertEqual(ConclusionOutcome.CONFLICT, compatibility.outcome)
        self.assertEqual(ConclusionOutcome.PASS, retry.outcome)

    def test_exact_conclusion_diff_and_version_scope(self) -> None:
        run = self.result.run
        self.assertEqual(("python_312_compatible",), run.recomputed_conclusions)
        self.assertEqual(("python_312_compatible@2",), run.created_conclusions)
        self.assertEqual(1, len(run.conclusion_diffs))
        diff = run.conclusion_diffs[0]
        self.assertEqual(
            ["EV_ADVISORY_PY312@1", "EV_RUNTIME_PY312@1"],
            diff["changed_evidence"],
        )
        self.assertEqual("pass", diff["old_version"]["outcome"])
        self.assertEqual("conflict", diff["new_version"]["outcome"])
        self.assertEqual("source_conflict", diff["change_reason"])

        with closing(sqlite3.connect(self.database_path)) as connection:
            retry_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'retry_policy_fit'"
            ).fetchone()[0]
            py312_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'python_312_compatible'"
            ).fetchone()[0]
            runtime_guide = connection.execute(
                "SELECT supersedes_version_id FROM source_versions WHERE version_id = 'SRC_RUNTIME_GUIDE@1.1'"
            ).fetchone()[0]
            advisory = connection.execute(
                "SELECT supersedes_version_id FROM source_versions WHERE version_id = 'SRC_SECURITY_ADVISORY@1.0'"
            ).fetchone()[0]
        self.assertEqual(1, retry_versions)
        self.assertEqual(2, py312_versions)
        self.assertIsNone(runtime_guide)
        self.assertIsNone(advisory)

    def test_trace_records_conflict_event(self) -> None:
        run = self.result.run
        event_types = [event["event_type"] for event in run.trace_events]
        self.assertIn("conflict_source_recorded", event_types)
        self.assertNotIn("old_evidence_expired", event_types)

    def test_metrics_match_full_recompute(self) -> None:
        metrics = self.result.metrics
        for name in (
            "candidate_impact_precision",
            "candidate_impact_recall",
            "invalidation_precision",
            "invalidation_recall",
            "unaffected_preservation",
        ):
            self.assertEqual(1.0, metrics[name])
        self.assertEqual(0.5, metrics["selective_recompute_ratio"])
        self.assertTrue(metrics["repair_success"])
        self.assertTrue(metrics["full_recompute_equivalent"])
        self.assertTrue(metrics["provenance_integrity"])
        self.assertEqual([], metrics["failures"])


if __name__ == "__main__":
    unittest.main()
