from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veritas.domain.enums import Assessment, ChangeType, ConclusionOutcome, EdgeType
from veritas.domain.models import (
    ChangeEvent,
    Claim,
    ClaimAssessment,
    ConclusionVersion,
    DependencyEdge,
    EvidenceSpan,
    SourceVersion,
)
from veritas.invalidation.repair import ChangePackage
from veritas.storage.sqlite import SQLiteRepository


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Scenario:
    raw: dict[str, Any]
    path: Path
    input_snapshot_hash: str

    @property
    def scenario_id(self) -> str:
        return self.raw["scenario_id"]

    @property
    def scenario_version(self) -> str:
        return self.raw["scenario_version"]

    @property
    def rule_version(self) -> str:
        return self.raw["rule_version"]

    @property
    def ground_truth(self) -> dict[str, Any]:
        return self.raw["ground_truth"]

    @property
    def ground_truth_hash(self) -> str:
        return sha256_text(canonical_json(self.ground_truth))


def load_scenario(path: str | Path) -> Scenario:
    scenario_path = Path(path)
    raw = json.loads(scenario_path.read_text(encoding="utf-8"))
    input_snapshot_hash = sha256_text(canonical_json(raw["t0"]))
    return Scenario(raw=raw, path=scenario_path, input_snapshot_hash=input_snapshot_hash)


def initialize_t0(repository: SQLiteRepository, scenario: Scenario) -> None:
    raw = scenario.raw
    t0 = raw["t0"]
    reasoned_at = raw["t0_reasoned_at"]
    with repository.transaction():
        registered_hash = repository.get_scenario_snapshot_hash(
            scenario.scenario_id,
            scenario.scenario_version,
            raw["input_snapshot_id"],
        )
        if registered_hash is not None:
            if registered_hash != scenario.input_snapshot_hash:
                raise ValueError("scenario snapshot identity is already registered with a different hash")
            return
        populated_tables = {
            table: count
            for table, count in repository.entity_counts().items()
            if table != "scenario_snapshots" and count
        }
        if populated_tables:
            event = raw["change"]["event"]
            legacy_run = repository.find_evolution_run(
                raw["project_id"],
                event["external_event_id"],
            )
            legacy_identity_matches = (
                legacy_run is not None
                and legacy_run.scenario_id == scenario.scenario_id
                and legacy_run.scenario_version == scenario.scenario_version
                and legacy_run.input_snapshot_id == raw["input_snapshot_id"]
                and legacy_run.input_snapshot_hash == scenario.input_snapshot_hash
                and legacy_run.rule_version == scenario.rule_version
            )
            if not legacy_identity_matches:
                raise ValueError(
                    "cannot initialize an unregistered scenario snapshot into a non-empty database"
                )
            repository.register_scenario_snapshot(
                scenario_id=scenario.scenario_id,
                scenario_version=scenario.scenario_version,
                input_snapshot_id=raw["input_snapshot_id"],
                input_snapshot_hash=scenario.input_snapshot_hash,
                loaded_at=reasoned_at,
            )
            return
        for source in t0["sources"]:
            repository.insert_source_version(_source(source))
        for evidence in t0["evidence"]:
            repository.insert_evidence_span(_evidence(evidence))
        for claim in t0["claims"]:
            repository.insert_claim(_claim(claim))
        for edge in t0["edges"]:
            repository.insert_dependency_edge(
                _edge(edge, created_at=reasoned_at, valid_from=reasoned_at, rule_version=scenario.rule_version)
            )
        for assessment in t0["assessments"]:
            repository.insert_claim_assessment(
                ClaimAssessment(
                    assessment_id=assessment["assessment_id"],
                    claim_id=assessment["claim_id"],
                    snapshot_id=raw["input_snapshot_id"],
                    assessment=Assessment(assessment["assessment"]),
                    rule_version=scenario.rule_version,
                    reason_refs=tuple(assessment["reason_refs"]),
                    reasoned_at=reasoned_at,
                )
            )
        for conclusion in t0["conclusions"]:
            repository.insert_conclusion_version(
                ConclusionVersion(
                    conclusion_key=conclusion["conclusion_key"],
                    conclusion_version_id=conclusion["conclusion_version_id"],
                    version_number=int(conclusion["version_number"]),
                    statement=conclusion["statement"],
                    outcome=ConclusionOutcome(conclusion["outcome"]),
                    dependency_rule=conclusion["dependency_rule"],
                    reason_refs=tuple(conclusion["reason_refs"]),
                    reasoned_at=reasoned_at,
                )
            )
        for edge in t0["conclusion_edges"]:
            repository.insert_dependency_edge(
                _edge(edge, created_at=reasoned_at, valid_from=reasoned_at, rule_version=scenario.rule_version)
            )
        repository.register_scenario_snapshot(
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.scenario_version,
            input_snapshot_id=raw["input_snapshot_id"],
            input_snapshot_hash=scenario.input_snapshot_hash,
            loaded_at=reasoned_at,
        )


def build_change_package(scenario: Scenario) -> ChangePackage:
    raw = scenario.raw
    change = raw["change"]
    event_raw = change["event"]
    event = ChangeEvent(
        change_event_id=event_raw["change_event_id"],
        external_event_id=event_raw["external_event_id"],
        project_id=raw["project_id"],
        change_type=ChangeType(event_raw["change_type"]),
        old_source_version_id=event_raw["old_source_version_id"],
        new_source_version_id=event_raw.get("new_source_version_id"),
        changed_locators=tuple(event_raw["changed_locators"]),
        observed_at=event_raw["observed_at"],
        effective_at=event_raw["effective_at"],
    )
    new_source_raw = change.get("new_source")
    return ChangePackage(
        scenario_id=scenario.scenario_id,
        scenario_version=scenario.scenario_version,
        input_snapshot_id=raw["input_snapshot_id"],
        input_snapshot_hash=scenario.input_snapshot_hash,
        rule_version=scenario.rule_version,
        event=event,
        new_source=None if new_source_raw is None else _source(new_source_raw),
        new_claims=tuple(_claim(item) for item in change.get("new_claims", [])),
        new_evidence=tuple(_evidence(item) for item in change.get("new_evidence", [])),
        new_edges=tuple(
            _edge(
                item,
                created_at=event.observed_at,
                valid_from=event.effective_at,
                rule_version=scenario.rule_version,
            )
            for item in change.get("new_edges", [])
        ),
    )


def _source(raw: dict[str, Any]) -> SourceVersion:
    return SourceVersion(
        source_id=raw["source_id"],
        version_id=raw["version_id"],
        version_label=raw["version_label"],
        canonical_uri=raw["canonical_uri"],
        content_hash=sha256_text(raw["content"]),
        published_at=raw.get("published_at"),
        observed_at=raw["observed_at"],
        valid_from=raw["valid_from"],
        valid_to=raw.get("valid_to"),
        supersedes_version_id=raw.get("supersedes_version_id"),
    )


def _evidence(raw: dict[str, Any]) -> EvidenceSpan:
    return EvidenceSpan(
        evidence_id=raw["evidence_id"],
        source_version_id=raw["source_version_id"],
        locator=raw["locator"],
        text=raw["text"],
        text_hash=sha256_text(raw["text"]),
        normalized_assertion=raw["normalized_assertion"],
        valid_from=raw["valid_from"],
        valid_to=raw.get("valid_to"),
    )


def _claim(raw: dict[str, Any]) -> Claim:
    return Claim(
        claim_id=raw["claim_id"],
        statement=raw["statement"],
        created_at=raw["created_at"],
        canonical_key=raw["canonical_key"],
    )


def _edge(
    raw: dict[str, Any],
    *,
    created_at: str,
    valid_from: str,
    rule_version: str,
) -> DependencyEdge:
    return DependencyEdge(
        edge_id=raw["edge_id"],
        edge_type=EdgeType(raw["edge_type"]),
        from_id=raw["from_id"],
        to_id=raw["to_id"],
        created_at=created_at,
        valid_from=valid_from,
        valid_to=None,
        rule_version=rule_version,
    )
