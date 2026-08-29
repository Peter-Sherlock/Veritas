from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.evaluation.scenario import canonical_json
from veritas.evaluation.suite_runner import (
    FAILURE_CODES,
    run_suite,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "datasets" / "suites" / "p0-evolution-suite-2.json"


class P0EvolutionSuite2Tests(unittest.TestCase):
    def test_manifest_declared_acceptance_and_aggregate_contract(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = run_suite(MANIFEST_PATH, root)
            summary = result.summary
            self.assertEqual(
                ["GS-001", "GS-002", "GS-003", "GS-004", "GS-005"],
                summary["scenario_order"],
            )
            self.assertEqual(0, summary["critical_failure_count"])
            self.assertEqual([], summary["failures"])
            self.assertEqual(
                {failure_code: 0 for failure_code in FAILURE_CODES},
                summary["failure_taxonomy_counts"],
            )
            self.assertTrue(summary["p0_3_acceptance_candidate"])
            self.assertNotIn("p0_2b_acceptance_candidate", summary)
            self.assertEqual(
                "P0-3_IMPLEMENTATION_VERIFICATION", summary["evaluation_status"]
            )
            self.assertEqual("pending_gate_p0_review", summary["gate_p0_decision"])

            for values in summary["per_scenario"].values():
                self.assertTrue(values["artifact_hash_valid"])
                self.assertEqual([], values["metrics"]["failures"])
            for value in summary["micro_aggregate"].values():
                self.assertEqual(1.0, value)

            totals = summary["recompute_totals"]
            self.assertEqual(4, totals["selective_recomputed_conclusions"])
            self.assertEqual(11, totals["selective_total_conclusions"])
            self.assertAlmostEqual(4 / 11, totals["selective_recompute_ratio"])
            self.assertEqual(11, totals["full_recomputed_conclusions"])
            self.assertEqual(1.0, totals["full_recompute_ratio"])

            payload = json.loads(result.summary_path.read_text(encoding="utf-8"))
            expected_hash = payload.pop("output_hash")
            actual_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
            self.assertEqual(expected_hash, actual_hash)

    def test_legacy_suite_behavior_is_unchanged(self) -> None:
        legacy_manifest = PROJECT_ROOT / "datasets" / "suites" / "p0-evolution-suite.json"
        with tempfile.TemporaryDirectory() as root:
            summary = run_suite(legacy_manifest, root).summary
            self.assertTrue(summary["p0_2b_acceptance_candidate"])
            self.assertNotIn("p0_3_acceptance_candidate", summary)
            self.assertEqual(
                "P0-2B_IMPLEMENTATION_VERIFICATION", summary["evaluation_status"]
            )
            self.assertEqual(2, summary["recompute_totals"]["selective_recomputed_conclusions"])
            self.assertEqual(6, summary["recompute_totals"]["selective_total_conclusions"])


if __name__ == "__main__":
    unittest.main()
