from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from veritas.evaluation.metrics import evaluate_run
from veritas.evaluation.runner import run_scenario
from veritas.evaluation.scenario import load_scenario


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-001" / "scenario.json"


class FailureTaxonomyCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        root = Path(cls.temp_directory.name)
        cls.scenario = load_scenario(SCENARIO_PATH)
        cls.result = run_scenario(
            SCENARIO_PATH,
            root / "veritas.sqlite3",
            root / "artifacts",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_directory.cleanup()

    def _evaluate(
        self,
        *,
        ground_truth: dict | None = None,
        replay_determinism: bool = True,
        event_idempotency: bool = True,
        provenance_errors: list[dict] | None = None,
    ) -> dict:
        return evaluate_run(
            self.result.run,
            deepcopy(self.scenario.ground_truth if ground_truth is None else ground_truth),
            current_outcomes=dict(self.result.metrics["current_outcomes"]),
            full_recompute_outcomes=dict(self.result.metrics["full_recompute_outcomes"]),
            replay_determinism=replay_determinism,
            event_idempotency=event_idempotency,
            provenance_errors=[] if provenance_errors is None else provenance_errors,
        )

    def _assert_single_failure(self, metrics: dict, failure_code: str) -> dict:
        self.assertEqual([failure_code], [item["failure_code"] for item in metrics["failures"]])
        failure = metrics["failures"][0]
        self.assertEqual("GS-001", failure["scenario_id"])
        self.assertEqual("critical", failure["severity"])
        self.assertTrue(failure["entity_refs"])
        self.assertIn("expected", failure)
        self.assertIn("actual", failure)
        self.assertTrue(failure["trace_refs"])
        return failure

    def test_f01_impact_detection_is_calibrated(self) -> None:
        ground_truth = deepcopy(self.scenario.ground_truth)
        ground_truth["reverify"].append("unexpected_node")

        failure = self._assert_single_failure(
            self._evaluate(ground_truth=ground_truth),
            "F01_IMPACT_DETECTION",
        )

        self.assertIn("unexpected_node", failure["entity_refs"])

    def test_f02_invalidation_decision_is_calibrated(self) -> None:
        ground_truth = deepcopy(self.scenario.ground_truth)
        ground_truth["semantic_change"] = []
        ground_truth["expected_recomputed_conclusions"] = list(
            self.result.run.recomputed_conclusions
        )

        self._assert_single_failure(
            self._evaluate(ground_truth=ground_truth),
            "F02_INVALIDATION_DECISION",
        )

    def test_f03_repair_correctness_is_calibrated(self) -> None:
        ground_truth = deepcopy(self.scenario.ground_truth)
        ground_truth["expected_outcomes"]["retry_policy_fit"] = "pass"

        self._assert_single_failure(
            self._evaluate(ground_truth=ground_truth),
            "F03_REPAIR_CORRECTNESS",
        )

    def test_f04_recompute_scope_is_calibrated(self) -> None:
        ground_truth = deepcopy(self.scenario.ground_truth)
        ground_truth["expected_recomputed_conclusions"] = []

        self._assert_single_failure(
            self._evaluate(ground_truth=ground_truth),
            "F04_RECOMPUTE_SCOPE",
        )

    def test_f05_provenance_integrity_is_calibrated(self) -> None:
        provenance_error = {
            "entity_id": "EDGE_MISSING",
            "entity_type": "dependency_edge",
            "error": "missing_dependency_edge",
        }

        failure = self._assert_single_failure(
            self._evaluate(provenance_errors=[provenance_error]),
            "F05_PROVENANCE_INTEGRITY",
        )

        self.assertEqual(["EDGE_MISSING"], failure["entity_refs"])

    def test_f06_replay_reproducibility_is_calibrated(self) -> None:
        self._assert_single_failure(
            self._evaluate(replay_determinism=False),
            "F06_REPLAY_REPRODUCIBILITY",
        )


if __name__ == "__main__":
    unittest.main()
