from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from veritas.domain.enums import Assessment
from veritas.evaluation.runner import run_scenario
from veritas.evaluation.scenario import (
    build_change_package,
    canonical_json,
    initialize_t0,
    load_scenario,
)
from veritas.invalidation.repair import EvolutionEngine
from veritas.storage.sqlite import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-001" / "scenario.json"
EXPECTED_ARTIFACTS = {
    "candidate_impact.json",
    "confirmed_invalidations.json",
    "conclusion_diff.json",
    "trace.json",
    "metrics.json",
}


class GoldenScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database_path = root / "veritas.sqlite3"
        self.result = run_scenario(SCENARIO_PATH, self.database_path, root / "artifacts")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_expected_metrics_and_full_recompute_baseline(self) -> None:
        metrics = self.result.metrics
        self.assertEqual(1.0, metrics["candidate_impact_precision"])
        self.assertEqual(1.0, metrics["candidate_impact_recall"])
        self.assertEqual(1.0, metrics["invalidation_precision"])
        self.assertEqual(1.0, metrics["invalidation_recall"])
        self.assertEqual(1.0, metrics["unaffected_preservation"])
        self.assertTrue(metrics["repair_success"])
        self.assertEqual(0.5, metrics["selective_recompute_ratio"])
        self.assertEqual(1.0, metrics["full_recompute_ratio"])
        self.assertTrue(metrics["full_recompute_equivalent"])

    def test_redundant_evidence_is_rechecked_but_not_invalidated(self) -> None:
        run = self.result.run
        self.assertEqual(("retry_supported",), run.rechecked_unchanged)
        invalidated = {item["node_key"] for item in run.confirmed_invalidations}
        self.assertEqual({"default_retries_3", "retry_policy_fit"}, invalidated)

        with SQLiteRepository(self.database_path) as repository:
            retry_assessment = repository.get_current_assessment("retry_supported")
            default_three = repository.get_current_assessment("default_retries_3")
            default_one = repository.get_current_assessment("default_retries_1")

        self.assertEqual(Assessment.ACCEPTED, retry_assessment.assessment)
        self.assertEqual(Assessment.CONTRADICTED, default_three.assessment)
        self.assertEqual(Assessment.ACCEPTED, default_one.assessment)
        self.assertIn("EDGE_GUIDE_RETRY_SUPPORT@1", retry_assessment.reason_refs)
        self.assertIn("EDGE_API_RETRY_SUPPORT@2", retry_assessment.reason_refs)

    def test_only_affected_conclusion_receives_a_new_version(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            retry_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'retry_policy_fit'"
            ).fetchone()[0]
            python_versions = connection.execute(
                "SELECT COUNT(*) FROM conclusion_versions WHERE conclusion_key = 'python_311_compatible'"
            ).fetchone()[0]
            old_source = connection.execute(
                "SELECT valid_to FROM source_versions WHERE version_id = 'SRC_API@1.0'"
            ).fetchone()
            new_source = connection.execute(
                "SELECT supersedes_version_id FROM source_versions WHERE version_id = 'SRC_API@1.1'"
            ).fetchone()

        self.assertEqual(2, retry_versions)
        self.assertEqual(1, python_versions)
        self.assertIsNone(old_source[0])
        self.assertEqual("SRC_API@1.0", new_source[0])
        self.assertEqual(("retry_policy_fit",), self.result.run.recomputed_conclusions)

    def test_event_replay_is_idempotent_and_deterministic(self) -> None:
        self.assertTrue(self.result.metrics["replay_determinism"])
        self.assertTrue(self.result.metrics["event_idempotency"])
        self.assertEqual(
            self.result.counts_after_first_run,
            self.result.counts_after_replay,
        )
        self.assertEqual(1, self.result.counts_after_replay["evolution_runs"])
        self.assertEqual(1, self.result.counts_after_replay["change_events"])

    def test_idempotency_key_rejects_scenario_drift(self) -> None:
        scenario = load_scenario(SCENARIO_PATH)
        package = build_change_package(scenario)
        with SQLiteRepository(self.database_path) as repository:
            initialize_t0(repository, scenario)
            drifted_package = replace(package, input_snapshot_hash="0" * 64)
            with self.assertRaisesRegex(ValueError, "idempotency key collision"):
                EvolutionEngine(repository).apply(drifted_package)

    def test_conclusion_diff_explains_the_semantic_change(self) -> None:
        diff = self.result.run.conclusion_diffs[0]
        self.assertEqual("retry_policy_fit@1", diff["old_version"]["conclusion_version_id"])
        self.assertEqual("retry_policy_fit@2", diff["new_version"]["conclusion_version_id"])
        self.assertEqual("pass", diff["old_version"]["outcome"])
        self.assertEqual("fail", diff["new_version"]["outcome"])
        self.assertEqual(
            ["EV_API_DEFAULT@1", "EV_API_DEFAULT@2"],
            diff["changed_evidence"],
        )
        self.assertEqual(
            ["default_retries_1", "default_retries_3"],
            diff["affected_claims"],
        )

    def test_artifacts_are_complete_and_content_addressed(self) -> None:
        actual_files = {path.name for path in self.result.artifact_dir.glob("*.json")}
        self.assertEqual(EXPECTED_ARTIFACTS, actual_files)
        for path in self.result.artifact_dir.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            output_hash = payload.pop("output_hash")
            expected_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
            self.assertEqual(expected_hash, output_hash, path.name)
            self.assertEqual("GS-001", payload["scenario_id"])
            self.assertEqual("1.0.0", payload["scenario_version"])
            self.assertEqual("p0-rules-1", payload["rule_version"])
            self.assertEqual(64, len(payload["input_snapshot_hash"]))

    def test_trace_contains_required_decision_events(self) -> None:
        event_types = {item["event_type"] for item in self.result.run.trace_events}
        required = {
            "change_event_received",
            "candidate_impact_computed",
            "source_version_activated",
            "old_evidence_expired",
            "claim_reverified",
            "claim_state_changed",
            "claim_state_unchanged",
            "conclusion_recomputed",
            "conclusion_version_created",
            "evolution_run_committed",
        }
        self.assertTrue(required.issubset(event_types))
        sequences = [item["event_seq"] for item in self.result.run.trace_events]
        self.assertEqual(list(range(1, len(sequences) + 1)), sequences)


if __name__ == "__main__":
    unittest.main()
