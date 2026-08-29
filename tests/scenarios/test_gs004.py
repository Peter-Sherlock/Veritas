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
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-004" / "scenario.json"


class ExpireScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database_path = root / "veritas.sqlite3"
        self.result = run_scenario(SCENARIO_PATH, self.database_path, root / "artifacts")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_only_expired_preview_branch_is_invalidated(self) -> None:
        run = self.result.run
        candidate = set(run.candidate_impact.claims) | set(run.candidate_impact.conclusions)
        invalidated = {item["node_key"] for item in run.confirmed_invalidations}
        self.assertEqual({"migration_assistance_available", "migration_assistance"}, candidate)
        self.assertEqual({"migration_assistance_available", "migration_assistance"}, invalidated)
        self.assertEqual(
            {
                "retry_supported",
                "default_retries_3",
                "python_311_supported",
                "policy_min_retries_3",
                "retry_policy_fit",
                "python_311_compatible",
            },
            set(run.untouched_nodes),
        )

    def test_expire_produces_unsupported_claim_and_unknown_conclusion(self) -> None:
        with SQLiteRepository(self.database_path) as repository:
            migration = repository.get_current_assessment("migration_assistance_available")
            assistance = repository.get_current_conclusion("migration_assistance")
            retry = repository.get_current_conclusion("retry_policy_fit")
            active_edges = repository.list_active_evidence_edges_for_claim(
                "migration_assistance_available"
            )
        self.assertEqual(Assessment.UNSUPPORTED, migration.assessment)
        self.assertEqual((), migration.reason_refs)
        self.assertEqual([], active_edges)
        self.assertEqual(ConclusionOutcome.UNKNOWN, assistance.outcome)
        self.assertEqual(ConclusionOutcome.PASS, retry.outcome)

    def test_exact_conclusion_diff_and_version_scope(self) -> None:
        run = self.result.run
        self.assertEqual(("migration_assistance",), run.recomputed_conclusions)
        self.assertEqual(("migration_assistance@2",), run.created_conclusions)
        self.assertEqual(1, len(run.conclusion_diffs))
        diff = run.conclusion_diffs[0]
        self.assertEqual(["EV_PREVIEW_MIGRATION@1"], diff["changed_evidence"])
        self.assertEqual("pass", diff["old_version"]["outcome"])
        self.assertEqual("unknown", diff["new_version"]["outcome"])
        self.assertEqual("source_expire", diff["change_reason"])

        with closing(sqlite3.connect(self.database_path)) as connection:
            retry_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'retry_policy_fit'"
            ).fetchone()[0]
            migration_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'migration_assistance'"
            ).fetchone()[0]
        self.assertEqual(1, retry_versions)
        self.assertEqual(2, migration_versions)

    def test_trace_records_expire_event_and_old_source_is_preserved(self) -> None:
        run = self.result.run
        event_types = [event["event_type"] for event in run.trace_events]
        self.assertIn("source_version_expired", event_types)
        with closing(sqlite3.connect(self.database_path)) as connection:
            preview_rows = connection.execute(
                "SELECT COUNT(*) FROM source_versions WHERE version_id = 'SRC_PREVIEW_PROGRAM@1.0'"
            ).fetchone()[0]
        self.assertEqual(1, preview_rows)

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
        self.assertAlmostEqual(1 / 3, metrics["selective_recompute_ratio"])
        self.assertTrue(metrics["repair_success"])
        self.assertTrue(metrics["full_recompute_equivalent"])
        self.assertTrue(metrics["provenance_integrity"])
        self.assertEqual([], metrics["failures"])


if __name__ == "__main__":
    unittest.main()
