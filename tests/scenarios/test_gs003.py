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
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-003" / "scenario.json"


class RuntimeCompatibilityScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database_path = root / "veritas.sqlite3"
        self.result = run_scenario(SCENARIO_PATH, self.database_path, root / "artifacts")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_only_python_311_branch_is_invalidated(self) -> None:
        run = self.result.run
        candidate = set(run.candidate_impact.claims) | set(run.candidate_impact.conclusions)
        invalidated = {item["node_key"] for item in run.confirmed_invalidations}
        self.assertEqual({"python_311_supported", "python_311_compatible"}, candidate)
        self.assertEqual({"python_311_supported", "python_311_compatible"}, invalidated)
        self.assertEqual(
            {
                "retry_supported",
                "default_retries_3",
                "policy_min_retries_3",
                "retry_policy_fit",
            },
            set(run.untouched_nodes),
        )

    def test_compatibility_rule_maps_contradiction_to_fail(self) -> None:
        with SQLiteRepository(self.database_path) as repository:
            py311 = repository.get_current_assessment("python_311_supported")
            py312 = repository.get_current_assessment("python_312_supported")
            compatibility = repository.get_current_conclusion("python_311_compatible")
            retry = repository.get_current_conclusion("retry_policy_fit")
        self.assertEqual(Assessment.CONTRADICTED, py311.assessment)
        self.assertEqual(("EDGE_RUNTIME_PY311_CONTRADICT@2",), py311.reason_refs)
        self.assertEqual(Assessment.ACCEPTED, py312.assessment)
        self.assertEqual(ConclusionOutcome.FAIL, compatibility.outcome)
        self.assertEqual(ConclusionOutcome.PASS, retry.outcome)

    def test_exact_conclusion_diff_and_version_scope(self) -> None:
        run = self.result.run
        self.assertEqual(("python_311_compatible",), run.recomputed_conclusions)
        self.assertEqual(("python_311_compatible@2",), run.created_conclusions)
        self.assertEqual(1, len(run.conclusion_diffs))
        diff = run.conclusion_diffs[0]
        self.assertEqual(
            ["EV_RUNTIME_PY311@1", "EV_RUNTIME_PY311@2"],
            diff["changed_evidence"],
        )
        self.assertEqual("pass", diff["old_version"]["outcome"])
        self.assertEqual("fail", diff["new_version"]["outcome"])

        with closing(sqlite3.connect(self.database_path)) as connection:
            retry_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'retry_policy_fit'"
            ).fetchone()[0]
            python_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'python_311_compatible'"
            ).fetchone()[0]
        self.assertEqual(1, retry_versions)
        self.assertEqual(2, python_versions)

    def test_metrics_match_full_recompute_without_touching_retry(self) -> None:
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
