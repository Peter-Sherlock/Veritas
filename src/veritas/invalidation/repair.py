from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from veritas.domain.enums import ChangeType, EdgeType
from veritas.domain.models import (
    ChangeEvent,
    Claim,
    ClaimAssessment,
    ConclusionVersion,
    DependencyEdge,
    EvidenceSpan,
    EvolutionRun,
    SourceVersion,
)
from veritas.evidence.rules import evaluate_claim, evaluate_conclusion
from veritas.invalidation.impact import propagate_change
from veritas.storage.sqlite import SQLiteRepository


@dataclass(frozen=True)
class ChangePackage:
    scenario_id: str
    scenario_version: str
    input_snapshot_id: str
    input_snapshot_hash: str
    rule_version: str
    event: ChangeEvent
    new_source: SourceVersion | None
    new_claims: tuple[Claim, ...]
    new_evidence: tuple[EvidenceSpan, ...]
    new_edges: tuple[DependencyEdge, ...]


class TraceBuilder:
    def __init__(self, run_id: str, base_timestamp: str, rule_version: str) -> None:
        self.run_id = run_id
        self.base_timestamp = datetime.fromisoformat(base_timestamp.replace("Z", "+00:00"))
        self.rule_version = rule_version
        self.events: list[dict[str, Any]] = []

    def add(self, event_type: str, entity_refs: Iterable[str], reason: dict[str, Any]) -> None:
        sequence = len(self.events) + 1
        timestamp = self.base_timestamp + timedelta(milliseconds=sequence)
        self.events.append(
            {
                "run_id": self.run_id,
                "event_seq": sequence,
                "event_type": event_type,
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "entity_refs": sorted(entity_refs),
                "rule_version": self.rule_version,
                "reason": reason,
            }
        )


class EvolutionEngine:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def _register_claim(self, claim: Claim) -> None:
        """Insert a new claim or reuse its frozen first-seen identity.

        Extraction timestamps describe when a candidate was observed. A later
        source version may therefore reconstruct the same deterministic claim
        id with a newer ``created_at``. The graph keeps the first registration
        timestamp, while statement/key disagreements remain hard conflicts.
        """
        try:
            existing = self.repository.get_claim(claim.claim_id)
        except KeyError:
            self.repository.insert_claim(claim)
            return
        if (
            existing.statement != claim.statement
            or existing.canonical_key != claim.canonical_key
        ):
            raise ValueError(f"claim_identity_conflict:{claim.claim_id}")

    def apply(self, package: ChangePackage) -> EvolutionRun:
        with self.repository.transaction():
            prior_run = self.repository.find_evolution_run(*package.event.idempotency_key)
            if prior_run is not None:
                self._validate_prior_run(prior_run, package)
                return prior_run

            self._validate_package(package)
            candidate = propagate_change(
                self.repository,
                package.event,
                new_edges=package.new_edges,
                new_evidence=package.new_evidence,
            )
            before_nodes = {
                *(claim.claim_id for claim in self.repository.list_claims()),
                *(conclusion.conclusion_key for conclusion in self.repository.list_current_conclusions()),
            }
            untouched_nodes = tuple(
                sorted(before_nodes - set(candidate.claims) - set(candidate.conclusions))
            )

            run_id = self._run_id(package)
            trace = TraceBuilder(run_id, package.event.observed_at, package.rule_version)
            trace.add(
                "change_event_received",
                (package.event.change_event_id,),
                {"change_type": package.event.change_type.value},
            )
            trace.add(
                "candidate_impact_computed",
                (*candidate.evidence_spans, *candidate.claims, *candidate.conclusions),
                {"source_snapshot": package.input_snapshot_id},
            )

            if package.new_source is not None:
                self.repository.insert_source_version(package.new_source)
            self.repository.insert_change_event(package.event)
            if package.event.change_type == ChangeType.RETRACT:
                trace.add(
                    "source_version_retracted",
                    (package.event.old_source_version_id,),
                    {"mode": "append_only_change_event"},
                )
            elif package.event.change_type == ChangeType.EXPIRE:
                trace.add(
                    "source_version_expired",
                    (package.event.old_source_version_id,),
                    {"mode": "append_only_change_event"},
                )
            elif package.event.change_type == ChangeType.CONFLICT:
                trace.add(
                    "conflict_source_recorded",
                    (package.new_source.version_id, package.event.old_source_version_id),
                    {"mode": "independent_source_no_supersession"},
                )
            else:
                trace.add(
                    "source_version_activated",
                    (package.new_source.version_id, package.event.old_source_version_id),
                    {"mode": "append_only_supersedes"},
                )
            if package.event.change_type != ChangeType.CONFLICT:
                trace.add(
                    "old_evidence_expired",
                    candidate.evidence_spans,
                    {"mode": "inactive_via_source_supersession"},
                )

            for claim in package.new_claims:
                self._register_claim(claim)
            for evidence in package.new_evidence:
                self.repository.insert_evidence_span(evidence)
            for edge in package.new_edges:
                self.repository.insert_dependency_edge(edge)

            reverification_results: list[dict[str, Any]] = []
            rechecked_unchanged: list[str] = []
            confirmed_invalidations: list[dict[str, Any]] = []
            created_assessments: list[str] = []
            semantic_changed_claims: set[str] = set()

            claims_to_assess = sorted(set(candidate.claims) | {claim.claim_id for claim in package.new_claims})
            output_snapshot_id = f"{package.scenario_id}:T1:{package.event.change_event_id}"
            for claim_id in claims_to_assess:
                previous = self.repository.get_current_assessment(claim_id)
                result = evaluate_claim(self.repository, claim_id)
                assessment = ClaimAssessment(
                    assessment_id=self.repository.next_assessment_id(claim_id),
                    claim_id=claim_id,
                    snapshot_id=output_snapshot_id,
                    assessment=result.assessment,
                    rule_version=package.rule_version,
                    reason_refs=result.reason_refs,
                    reasoned_at=package.event.observed_at,
                    supersedes_assessment_id=None if previous is None else previous.assessment_id,
                )
                self.repository.insert_claim_assessment(assessment)
                created_assessments.append(assessment.assessment_id)
                old_state = None if previous is None else previous.assessment.value
                new_state = assessment.assessment.value
                reverification_results.append(
                    {
                        "node_key": claim_id,
                        "old_state": old_state,
                        "new_state": new_state,
                        "assessment_id": assessment.assessment_id,
                        "reason_refs": list(assessment.reason_refs),
                    }
                )
                trace.add(
                    "claim_reverified",
                    (claim_id, assessment.assessment_id),
                    {"old_state": old_state, "new_state": new_state},
                )

                if previous is None:
                    trace.add(
                        "claim_state_created",
                        (claim_id,),
                        {"new_state": new_state},
                    )
                elif previous.assessment == assessment.assessment:
                    rechecked_unchanged.append(claim_id)
                    trace.add(
                        "claim_state_unchanged",
                        (claim_id,),
                        {"state": new_state, "evidence_rebased": True},
                    )
                else:
                    semantic_changed_claims.add(claim_id)
                    change = {
                        "node_key": claim_id,
                        "old_state": old_state,
                        "new_state": new_state,
                    }
                    confirmed_invalidations.append(change)
                    trace.add(
                        "claim_state_changed",
                        (claim_id,),
                        {"old_state": old_state, "new_state": new_state},
                    )

            created_conclusions: list[str] = []
            recomputed_conclusions: list[str] = []
            conclusion_diffs: list[dict[str, Any]] = []
            for conclusion_key in candidate.conclusions:
                old_conclusion = self.repository.get_current_conclusion(conclusion_key)
                old_dependency_claims = {
                    edge.from_id
                    for edge in self.repository.list_dependency_edges()
                    if edge.edge_type == EdgeType.DEPENDS_ON
                    and edge.to_id == old_conclusion.conclusion_version_id
                }
                if not old_dependency_claims.intersection(semantic_changed_claims):
                    continue

                recomputed_conclusions.append(conclusion_key)
                result = evaluate_conclusion(self.repository, old_conclusion)
                trace.add(
                    "conclusion_recomputed",
                    (conclusion_key, *result.dependency_claim_ids),
                    {"old_state": old_conclusion.outcome.value, "new_state": result.outcome.value},
                )
                if result.outcome == old_conclusion.outcome and result.statement == old_conclusion.statement:
                    continue

                version_number, version_id = self.repository.next_conclusion_version(conclusion_key)
                new_conclusion = ConclusionVersion(
                    conclusion_key=conclusion_key,
                    conclusion_version_id=version_id,
                    version_number=version_number,
                    statement=result.statement,
                    outcome=result.outcome,
                    dependency_rule=old_conclusion.dependency_rule,
                    reason_refs=result.reason_refs,
                    reasoned_at=package.event.observed_at,
                    supersedes_conclusion_version_id=old_conclusion.conclusion_version_id,
                )
                self.repository.insert_conclusion_version(new_conclusion)
                for claim_id in result.dependency_claim_ids:
                    self.repository.insert_dependency_edge(
                        DependencyEdge(
                            edge_id=f"EDGE_{claim_id}_TO_{version_id}",
                            edge_type=EdgeType.DEPENDS_ON,
                            from_id=claim_id,
                            to_id=version_id,
                            created_at=package.event.observed_at,
                            valid_from=package.event.effective_at,
                            valid_to=None,
                            rule_version=package.rule_version,
                        )
                    )
                created_conclusions.append(version_id)
                confirmed_invalidations.append(
                    {
                        "node_key": conclusion_key,
                        "old_state": old_conclusion.outcome.value,
                        "new_state": new_conclusion.outcome.value,
                    }
                )

                replacement_claims = set(result.dependency_claim_ids) - old_dependency_claims
                affected_claims = sorted(semantic_changed_claims | replacement_claims)
                changed_evidence = self._changed_evidence_for_claims(
                    affected_claims,
                    package.event.old_source_version_id,
                    None if package.new_source is None else package.new_source.version_id,
                )
                conclusion_diffs.append(
                    {
                        "change_event_id": package.event.change_event_id,
                        "conclusion_key": conclusion_key,
                        "old_version": {
                            "conclusion_version_id": old_conclusion.conclusion_version_id,
                            "outcome": old_conclusion.outcome.value,
                            "statement": old_conclusion.statement,
                        },
                        "changed_evidence": changed_evidence,
                        "affected_claims": affected_claims,
                        "new_version": {
                            "conclusion_version_id": new_conclusion.conclusion_version_id,
                            "outcome": new_conclusion.outcome.value,
                            "statement": new_conclusion.statement,
                        },
                        "change_reason": self._change_reason(package.event.change_type),
                        "action_required": True,
                    }
                )
                trace.add(
                    "conclusion_version_created",
                    (old_conclusion.conclusion_version_id, new_conclusion.conclusion_version_id),
                    {"relation": "supersedes"},
                )

            operational_metrics = {
                "candidate_claim_count": len(candidate.claims),
                "candidate_conclusion_count": len(candidate.conclusions),
                "recomputed_conclusion_count": len(recomputed_conclusions),
                "created_conclusion_count": len(created_conclusions),
            }
            trace.add(
                "evolution_run_committed",
                (run_id,),
                operational_metrics,
            )
            run = EvolutionRun(
                run_id=run_id,
                scenario_id=package.scenario_id,
                scenario_version=package.scenario_version,
                project_id=package.event.project_id,
                external_event_id=package.event.external_event_id,
                change_event_id=package.event.change_event_id,
                input_snapshot_id=package.input_snapshot_id,
                input_snapshot_hash=package.input_snapshot_hash,
                rule_version=package.rule_version,
                candidate_impact=candidate,
                reverification_results=tuple(reverification_results),
                rechecked_unchanged=tuple(sorted(rechecked_unchanged)),
                confirmed_invalidations=tuple(
                    sorted(confirmed_invalidations, key=lambda item: item["node_key"])
                ),
                created_claims=tuple(sorted(claim.claim_id for claim in package.new_claims)),
                created_claim_assessments=tuple(sorted(created_assessments)),
                created_conclusions=tuple(sorted(created_conclusions)),
                recomputed_conclusions=tuple(sorted(recomputed_conclusions)),
                untouched_nodes=untouched_nodes,
                conclusion_diffs=tuple(conclusion_diffs),
                trace_events=tuple(trace.events),
                operational_metrics=operational_metrics,
            )
            self.repository.insert_evolution_run(run)
            return run

    def _validate_package(self, package: ChangePackage) -> None:
        if not self.repository.source_version_exists(package.event.old_source_version_id):
            raise ValueError("old source version does not exist")
        if package.event.change_type in (ChangeType.RETRACT, ChangeType.EXPIRE):
            if package.event.new_source_version_id is not None or package.new_source is not None:
                raise ValueError("a retract/expire event cannot provide a new source version")
            if package.new_claims or package.new_evidence or package.new_edges:
                raise ValueError("a retract/expire event cannot introduce new claims, evidence, or edges")
            return
        if package.event.change_type == ChangeType.CONFLICT:
            if package.new_source is None:
                raise ValueError("a conflict event requires a new source version")
            if package.event.new_source_version_id != package.new_source.version_id:
                raise ValueError("event and package disagree on new source version")
            if package.new_source.supersedes_version_id is not None:
                raise ValueError("a conflicting source must be independent, not a superseding version")
            old_source = self.repository.get_source_version(package.event.old_source_version_id)
            if old_source.source_id == package.new_source.source_id:
                raise ValueError("a conflict event requires a different source identity")
            if not package.new_evidence or not package.new_edges:
                raise ValueError("a conflict event requires new evidence and new edges")
            return
        if package.event.change_type not in (ChangeType.REVISE, ChangeType.SUPERSEDE):
            raise ValueError(f"unsupported change type: {package.event.change_type.value}")
        if package.new_source is None:
            raise ValueError("a revised source requires a new source version")
        if package.event.new_source_version_id != package.new_source.version_id:
            raise ValueError("event and package disagree on new source version")
        if package.new_source.supersedes_version_id != package.event.old_source_version_id:
            raise ValueError("new source must supersede the event's old source")
        old_source = self.repository.get_source_version(package.event.old_source_version_id)
        if old_source.source_id != package.new_source.source_id:
            raise ValueError("source identity cannot change across versions")
        if old_source.content_hash == package.new_source.content_hash:
            raise ValueError("a revised source must have a different content hash")

    def _validate_prior_run(self, prior_run: EvolutionRun, package: ChangePackage) -> None:
        expected = (
            package.scenario_id,
            package.scenario_version,
            package.event.change_event_id,
            package.input_snapshot_hash,
            package.rule_version,
        )
        actual = (
            prior_run.scenario_id,
            prior_run.scenario_version,
            prior_run.change_event_id,
            prior_run.input_snapshot_hash,
            prior_run.rule_version,
        )
        if actual != expected:
            raise ValueError("idempotency key collision with a different scenario or rule version")
        if package.new_source is not None:
            stored_new_source = self.repository.get_source_version(package.new_source.version_id)
            if stored_new_source.content_hash != package.new_source.content_hash:
                raise ValueError("idempotency key collision with different source content")

    def _changed_evidence_for_claims(
        self,
        claim_ids: list[str],
        old_source_version_id: str,
        new_source_version_id: str | None,
    ) -> list[str]:
        relevant_evidence_ids = {
            edge.from_id
            for edge in self.repository.list_dependency_edges()
            if edge.edge_type in (EdgeType.SUPPORTS, EdgeType.CONTRADICTS)
            and edge.to_id in claim_ids
        }
        changed_source_evidence = {
            evidence.evidence_id
            for version_id in (old_source_version_id, new_source_version_id)
            if version_id is not None
            for evidence in self.repository.list_evidence_for_source(version_id)
        }
        return sorted(relevant_evidence_ids & changed_source_evidence)

    @staticmethod
    def _change_reason(change_type: ChangeType) -> str:
        if change_type == ChangeType.REVISE:
            return "source_revision"
        return f"source_{change_type.value}"

    @staticmethod
    def _run_id(package: ChangePackage) -> str:
        raw = "|".join(
            (
                package.scenario_id,
                package.scenario_version,
                package.event.project_id,
                package.event.external_event_id,
                package.input_snapshot_hash,
                package.rule_version,
            )
        )
        return f"run-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
