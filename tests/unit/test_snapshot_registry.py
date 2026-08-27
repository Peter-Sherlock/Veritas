from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from veritas.evaluation.scenario import (
    Scenario,
    _source,
    canonical_json,
    initialize_t0,
    load_scenario,
    sha256_text,
)
from veritas.storage.sqlite import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-002" / "scenario.json"


class ScenarioSnapshotRegistryTests(unittest.TestCase):
    def test_identical_snapshot_is_idempotent_but_hash_drift_is_rejected(self) -> None:
        scenario = load_scenario(SCENARIO_PATH)
        with tempfile.TemporaryDirectory() as root:
            with SQLiteRepository(Path(root) / "veritas.sqlite3") as repository:
                initialize_t0(repository, scenario)
                counts = repository.entity_counts()
                initialize_t0(repository, scenario)
                self.assertEqual(counts, repository.entity_counts())

                changed_raw = copy.deepcopy(scenario.raw)
                changed_raw["t0"]["claims"][0]["statement"] = "fixture drift"
                changed = Scenario(
                    raw=changed_raw,
                    path=scenario.path,
                    input_snapshot_hash=sha256_text(canonical_json(changed_raw["t0"])),
                )
                with self.assertRaisesRegex(ValueError, "different hash"):
                    initialize_t0(repository, changed)
                self.assertEqual(1, repository.entity_counts()["scenario_snapshots"])

    def test_unregistered_partial_database_is_rejected(self) -> None:
        scenario = load_scenario(SCENARIO_PATH)
        with tempfile.TemporaryDirectory() as root:
            with SQLiteRepository(Path(root) / "veritas.sqlite3") as repository:
                repository.insert_source_version(
                    # A valid fixture entity is enough to simulate a partial prior load.
                    _source(scenario.raw["t0"]["sources"][0])
                )
                with self.assertRaisesRegex(ValueError, "non-empty database"):
                    initialize_t0(repository, scenario)
                self.assertEqual(0, repository.entity_counts()["scenario_snapshots"])

    def test_provenance_audit_rejects_cross_source_supersedes(self) -> None:
        scenario = load_scenario(SCENARIO_PATH)
        with tempfile.TemporaryDirectory() as root:
            with SQLiteRepository(Path(root) / "veritas.sqlite3") as repository:
                initialize_t0(repository, scenario)
                original = repository.get_source_version("SRC_API@1.0")
                repository.insert_source_version(
                    replace(
                        original,
                        source_id="SRC_OTHER",
                        version_id="SRC_OTHER@2.0",
                        version_label="2.0",
                        content_hash=sha256_text("cross-source lineage"),
                        supersedes_version_id=original.version_id,
                    )
                )
                errors = repository.validate_provenance()
        self.assertIn(
            "source_supersedes_different_identity",
            {item["error"] for item in errors},
        )


if __name__ == "__main__":
    unittest.main()
