from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from veritas.domain.enums import ChangeType
from veritas.domain.models import ChangeEvent, Claim
from veritas.evaluation.scenario import build_change_package, initialize_t0, load_scenario
from veritas.invalidation.impact import propagate_change
from veritas.storage.sqlite import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-001" / "scenario.json"


class DomainValidationTests(unittest.TestCase):
    def test_domain_model_rejects_naive_datetime(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            Claim(
                claim_id="claim",
                statement="A claim",
                created_at="2026-08-27T10:00:00",
                canonical_key="claim.key",
            )

    def test_change_event_idempotency_key_is_project_scoped(self) -> None:
        event = ChangeEvent(
            change_event_id="change-1",
            external_event_id="external-1",
            project_id="project-1",
            change_type=ChangeType.REVISE,
            old_source_version_id="source@1",
            new_source_version_id="source@2",
            changed_locators=(),
            observed_at="2026-08-27T10:00:00Z",
            effective_at="2026-08-27T09:00:00Z",
        )
        self.assertEqual(("project-1", "external-1"), event.idempotency_key)

    def test_repository_rejects_same_immutable_id_with_different_payload(self) -> None:
        scenario = load_scenario(SCENARIO_PATH)
        with tempfile.TemporaryDirectory() as temporary_directory:
            with SQLiteRepository(
                Path(temporary_directory) / "veritas.sqlite3"
            ) as repository:
                initialize_t0(repository, scenario)
                claim = repository.get_claim("default_retries_3")
                repository.insert_claim(claim)
                with self.assertRaisesRegex(
                    ValueError, "immutable_entity_conflict:claims"
                ):
                    repository.insert_claim(
                        replace(claim, statement="A conflicting statement")
                    )


class CandidateImpactTests(unittest.TestCase):
    def test_gs001_candidate_impact_is_computed_from_t0(self) -> None:
        scenario = load_scenario(SCENARIO_PATH)
        package = build_change_package(scenario)
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "veritas.sqlite3"
            with SQLiteRepository(database) as repository:
                initialize_t0(repository, scenario)
                candidate = propagate_change(repository, package.event)

        self.assertEqual(
            ("EV_API_DEFAULT@1", "EV_API_RETRY@1"),
            candidate.evidence_spans,
        )
        self.assertEqual(
            ("default_retries_3", "retry_supported"),
            candidate.claims,
        )
        self.assertEqual(("retry_policy_fit",), candidate.conclusions)
        self.assertNotIn("python_311_compatible", candidate.conclusions)


if __name__ == "__main__":
    unittest.main()
