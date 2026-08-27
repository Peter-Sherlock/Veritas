from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.evaluation.scenario import canonical_json
from veritas.evaluation.suite_runner import run_suite, verify_artifact_directory


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "datasets" / "suites" / "p0-evolution-suite.json"


class P0EvolutionSuiteTests(unittest.TestCase):
    def test_explicit_manifest_and_aggregate_contract(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = run_suite(MANIFEST_PATH, root)
            summary = result.summary
            self.assertEqual(["GS-001", "GS-002", "GS-003"], summary["scenario_order"])
            self.assertEqual(0, summary["critical_failure_count"])
            self.assertEqual([], summary["failures"])
            self.assertTrue(summary["p0_2b_acceptance_candidate"])
            self.assertEqual("not_evaluated_in_p0_2b", summary["gate_p0_decision"])

            for values in summary["per_scenario"].values():
                self.assertTrue(values["artifact_hash_valid"])
                self.assertEqual([], values["metrics"]["failures"])
            for value in summary["micro_aggregate"].values():
                self.assertEqual(1.0, value)

            totals = summary["recompute_totals"]
            self.assertEqual(2, totals["selective_recomputed_conclusions"])
            self.assertEqual(6, totals["selective_total_conclusions"])
            self.assertAlmostEqual(2 / 6, totals["selective_recompute_ratio"])
            self.assertEqual(6, totals["full_recomputed_conclusions"])
            self.assertEqual(1.0, totals["full_recompute_ratio"])

            payload = json.loads(result.summary_path.read_text(encoding="utf-8"))
            expected_hash = payload.pop("output_hash")
            actual_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
            self.assertEqual(expected_hash, actual_hash)

    def test_artifact_hash_verifier_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = run_suite(MANIFEST_PATH, root)
            artifact_dir = result.scenario_results[0].artifact_dir
            artifact = artifact_dir / "candidate_impact.json"
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            payload["scenario_id"] = "tampered"
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            errors = verify_artifact_directory(artifact_dir)
            self.assertEqual("candidate_impact.json", errors[0]["artifact"])
            self.assertEqual("output_hash_mismatch", errors[0]["error"])
            artifact.unlink()
            missing_errors = verify_artifact_directory(artifact_dir)
            self.assertEqual("candidate_impact.json", missing_errors[0]["artifact"])
            self.assertEqual("missing_artifact", missing_errors[0]["error"])


if __name__ == "__main__":
    unittest.main()
