"""Research refresh: re-research results re-enter the graph (M3-2).

A revision event deactivates evidence through supersession and the
watching conclusions flip to unknown. Re-research (the planner's session,
M3-1) produces new evidence for the *current* source versions — but there
is no source change to attach a ChangeEvent to, so the P0 engine's
``apply`` does not fit. The refresh applier is the missing half of the
loop: it inserts the re-research bundle's claims/evidence/edges (all
grounded in active source versions), re-assesses every touched claim and
recomputes every affected conclusion under the engine's exact transition
contract (semantic-change intersection, statement comparison, superseding
conclusion version with fresh DEPENDS_ON edges).

Every refresh carries a deterministic id derived from the session and the
bundle content; re-applying the same refresh returns the stored result
without touching the graph (the engine's idempotency discipline, adapted
to a no-change-event operation). The audit row lives in its own
``research_refreshes`` table — a refresh is not a change event and never
masquerades as one in the change log.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from veritas.domain.enums import EdgeType
from veritas.domain.models import (
    ClaimAssessment,
    ConclusionVersion,
    DependencyEdge,
)
from veritas.evidence.rules import evaluate_claim, evaluate_conclusion
from veritas.extraction.models import ExtractionCandidateBundle
from veritas.storage.sqlite import SQLiteRepository


class RefreshError(ValueError):
    """A stable, classifiable failure of the research refresh."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def refresh_id_for(
    session_id: str,
    bundle: ExtractionCandidateBundle,
    rule_version: str,
) -> str:
    """Deterministic identity over the semantic refresh payload.

    The identity covers exactly what the refresh writes into the graph —
    claims, evidence and edges — plus the session and rule version. The
    bundle's ``documents`` are deliberately excluded: their token-usage
    fields are billing metadata of the extraction call, not semantics,
    and folding them into the identity would make the same semantic
    refresh unreplayable whenever usage numbers differ between a live
    call and its replay.
    """
    identity = {
        "session_id": session_id,
        "rule_version": rule_version,
        "claims": [claim.to_dict() for claim in bundle.claims],
        "evidence_spans": [evidence.to_dict() for evidence in bundle.evidence_spans],
        "edges": [edge.to_dict() for edge in bundle.edges],
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"refresh-{digest}"


def _validate_bundle(repository: SQLiteRepository, bundle: ExtractionCandidateBundle) -> None:
    if not bundle.claims and not bundle.evidence_spans and not bundle.edges:
        raise RefreshError("empty_refresh_bundle", "the re-research bundle is empty")
    for evidence in bundle.evidence_spans:
        version_id = evidence.source_version_id
        if not repository.source_version_exists(version_id):
            raise RefreshError(
                "unregistered_source",
                f"evidence {evidence.evidence_id!r} references unregistered "
                f"source {version_id!r}; register the corpus sources first",
            )
        if not repository.source_is_active(version_id):
            raise RefreshError(
                "superseded_source",
                f"evidence {evidence.evidence_id!r} grounds in inactive source "
                f"{version_id!r}; re-research must use the current version view",
            )


def _dependency_claims(
    repository: SQLiteRepository, conclusion_version_id: str
) -> set[str]:
    return {
        edge.from_id
        for edge in repository.list_dependency_edges()
        if edge.edge_type == EdgeType.DEPENDS_ON
        and edge.to_id == conclusion_version_id
    }


def apply_research_refresh(
    repository: SQLiteRepository,
    *,
    bundle: ExtractionCandidateBundle,
    session_id: str,
    rule_version: str,
    refreshed_at: str,
) -> dict[str, Any]:
    """Insert re-research output and run the engine's transition contract."""
    refresh_id = refresh_id_for(session_id, bundle, rule_version)
    with repository.transaction():
        prior = repository.find_research_refresh(refresh_id)
        if prior is not None:
            return prior
        _validate_bundle(repository, bundle)

        for claim in bundle.claims:
            repository.insert_claim(claim)
        for evidence in bundle.evidence_spans:
            repository.insert_evidence_span(evidence)
        for edge in bundle.edges:
            repository.insert_dependency_edge(edge)

        claims_to_assess = sorted(
            {claim.claim_id for claim in bundle.claims}
            | {
                edge.to_id
                for edge in bundle.edges
                if edge.edge_type in (EdgeType.SUPPORTS, EdgeType.CONTRADICTS)
            }
        )
        reassessed: list[dict[str, Any]] = []
        rechecked_unchanged: list[str] = []
        semantic_changed: set[str] = set()
        for claim_id in claims_to_assess:
            previous = repository.get_current_assessment(claim_id)
            result = evaluate_claim(repository, claim_id)
            assessment = ClaimAssessment(
                assessment_id=repository.next_assessment_id(claim_id),
                claim_id=claim_id,
                snapshot_id=f"refresh:{session_id}",
                assessment=result.assessment,
                rule_version=rule_version,
                reason_refs=result.reason_refs,
                reasoned_at=refreshed_at,
                supersedes_assessment_id=(
                    None if previous is None else previous.assessment_id
                ),
            )
            repository.insert_claim_assessment(assessment)
            old_state = None if previous is None else previous.assessment.value
            new_state = result.assessment.value
            reassessed.append(
                {"claim_id": claim_id, "old_state": old_state, "new_state": new_state}
            )
            if previous is None or previous.assessment != result.assessment:
                semantic_changed.add(claim_id)
            else:
                rechecked_unchanged.append(claim_id)

        recomputed_conclusions: list[str] = []
        created_conclusions: list[str] = []
        for conclusion in repository.list_current_conclusions():
            dependencies = _dependency_claims(
                repository, conclusion.conclusion_version_id
            )
            if not dependencies.intersection(semantic_changed):
                continue
            result = evaluate_conclusion(repository, conclusion)
            recomputed_conclusions.append(conclusion.conclusion_key)
            if result.outcome == conclusion.outcome and result.statement == conclusion.statement:
                continue
            version_number, version_id = repository.next_conclusion_version(
                conclusion.conclusion_key
            )
            new_conclusion = ConclusionVersion(
                conclusion_key=conclusion.conclusion_key,
                conclusion_version_id=version_id,
                version_number=version_number,
                statement=result.statement,
                outcome=result.outcome,
                dependency_rule=conclusion.dependency_rule,
                reason_refs=result.reason_refs,
                reasoned_at=refreshed_at,
                supersedes_conclusion_version_id=conclusion.conclusion_version_id,
            )
            repository.insert_conclusion_version(new_conclusion)
            for claim_id in result.dependency_claim_ids:
                repository.insert_dependency_edge(
                    DependencyEdge(
                        edge_id=f"EDGE_{claim_id}_TO_{version_id}",
                        edge_type=EdgeType.DEPENDS_ON,
                        from_id=claim_id,
                        to_id=version_id,
                        created_at=refreshed_at,
                        valid_from=refreshed_at,
                        valid_to=None,
                        rule_version=rule_version,
                    )
                )
            created_conclusions.append(version_id)

        payload = {
            "refresh_id": refresh_id,
            "session_id": session_id,
            "refreshed_at": refreshed_at,
            "rule_version": rule_version,
            "reassessed_claims": reassessed,
            "rechecked_unchanged": rechecked_unchanged,
            "semantic_changed_claims": sorted(semantic_changed),
            "recomputed_conclusions": recomputed_conclusions,
            "created_conclusions": created_conclusions,
        }
        repository.insert_research_refresh(refresh_id, session_id, refreshed_at, payload)
        return payload
