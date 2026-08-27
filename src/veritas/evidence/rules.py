from __future__ import annotations

from dataclasses import dataclass

from veritas.domain.enums import Assessment, ConclusionOutcome, EdgeType
from veritas.domain.models import ConclusionVersion
from veritas.storage.sqlite import SQLiteRepository


@dataclass(frozen=True)
class ClaimRuleResult:
    assessment: Assessment
    reason_refs: tuple[str, ...]


@dataclass(frozen=True)
class ConclusionRuleResult:
    outcome: ConclusionOutcome
    statement: str
    dependency_claim_ids: tuple[str, ...]
    reason_refs: tuple[str, ...]


def evaluate_claim(repository: SQLiteRepository, claim_id: str) -> ClaimRuleResult:
    edges = repository.list_active_evidence_edges_for_claim(claim_id)
    supports = tuple(sorted(edge.edge_id for edge in edges if edge.edge_type == EdgeType.SUPPORTS))
    contradictions = tuple(
        sorted(edge.edge_id for edge in edges if edge.edge_type == EdgeType.CONTRADICTS)
    )

    if supports and contradictions:
        assessment = Assessment.CONFLICT
    elif supports:
        assessment = Assessment.ACCEPTED
    elif contradictions:
        assessment = Assessment.CONTRADICTED
    else:
        assessment = Assessment.UNSUPPORTED
    return ClaimRuleResult(assessment=assessment, reason_refs=supports + contradictions)


def evaluate_conclusion(
    repository: SQLiteRepository,
    conclusion: ConclusionVersion,
) -> ConclusionRuleResult:
    kind = conclusion.dependency_rule.get("kind")
    if kind == "all_accepted":
        return _evaluate_all_accepted(repository, conclusion)
    if kind == "numeric_threshold":
        return _evaluate_numeric_threshold(repository, conclusion)
    if kind == "compatibility_support":
        return _evaluate_compatibility_support(repository, conclusion)
    raise ValueError(f"unsupported conclusion rule: {kind}")


def _evaluate_all_accepted(
    repository: SQLiteRepository,
    conclusion: ConclusionVersion,
) -> ConclusionRuleResult:
    rule = conclusion.dependency_rule
    claim_ids = tuple(rule["claim_ids"])
    assessments = [repository.get_current_assessment(claim_id) for claim_id in claim_ids]
    if any(item is None for item in assessments):
        outcome = ConclusionOutcome.UNKNOWN
    elif any(item.assessment == Assessment.CONFLICT for item in assessments if item is not None):
        outcome = ConclusionOutcome.CONFLICT
    elif all(item.assessment == Assessment.ACCEPTED for item in assessments if item is not None):
        outcome = ConclusionOutcome.PASS
    else:
        outcome = ConclusionOutcome.UNKNOWN

    statement = rule["pass_statement"] if outcome == ConclusionOutcome.PASS else rule["fail_statement"]
    reason_refs = tuple(item.assessment_id for item in assessments if item is not None)
    return ConclusionRuleResult(outcome, statement, claim_ids, reason_refs)


def _evaluate_numeric_threshold(
    repository: SQLiteRepository,
    conclusion: ConclusionVersion,
) -> ConclusionRuleResult:
    rule = conclusion.dependency_rule
    capability_claim_id = rule["capability_claim_id"]
    policy_claim_id = rule["policy_claim_id"]
    prefix = rule["value_claim_prefix"]
    minimum = int(rule["minimum"])

    capability = repository.get_current_assessment(capability_claim_id)
    policy = repository.get_current_assessment(policy_claim_id)
    value_candidates: list[tuple[int, str, str]] = []
    for claim in repository.list_claims():
        if not claim.canonical_key.startswith(prefix):
            continue
        assessment = repository.get_current_assessment(claim.claim_id)
        if assessment is None or assessment.assessment != Assessment.ACCEPTED:
            continue
        raw_value = claim.canonical_key.removeprefix(prefix)
        try:
            numeric_value = int(raw_value)
        except ValueError as exc:
            raise ValueError(f"non-numeric threshold claim: {claim.canonical_key}") from exc
        value_candidates.append((numeric_value, claim.claim_id, assessment.assessment_id))

    fixed_refs = tuple(
        item.assessment_id for item in (capability, policy) if item is not None
    )
    fixed_claims = (capability_claim_id, policy_claim_id)

    if any(
        item is not None and item.assessment == Assessment.CONFLICT
        for item in (capability, policy)
    ) or len(value_candidates) > 1:
        return ConclusionRuleResult(
            ConclusionOutcome.CONFLICT,
            rule["fail_statement"],
            fixed_claims + tuple(item[1] for item in value_candidates),
            fixed_refs + tuple(item[2] for item in value_candidates),
        )

    fixed_are_accepted = all(
        item is not None and item.assessment == Assessment.ACCEPTED
        for item in (capability, policy)
    )
    if not fixed_are_accepted or not value_candidates:
        return ConclusionRuleResult(
            ConclusionOutcome.UNKNOWN,
            rule["fail_statement"],
            fixed_claims,
            fixed_refs,
        )

    value, value_claim_id, value_assessment_id = value_candidates[0]
    outcome = ConclusionOutcome.PASS if value >= minimum else ConclusionOutcome.FAIL
    statement = rule["pass_statement"] if outcome == ConclusionOutcome.PASS else rule["fail_statement"]
    return ConclusionRuleResult(
        outcome,
        statement,
        (capability_claim_id, value_claim_id, policy_claim_id),
        (
            capability.assessment_id,
            value_assessment_id,
            policy.assessment_id,
        ),
    )


def _evaluate_compatibility_support(
    repository: SQLiteRepository,
    conclusion: ConclusionVersion,
) -> ConclusionRuleResult:
    rule = conclusion.dependency_rule
    claim_id = rule["claim_id"]
    assessment = repository.get_current_assessment(claim_id)
    if assessment is None or assessment.assessment == Assessment.UNSUPPORTED:
        outcome = ConclusionOutcome.UNKNOWN
        statement = rule["unknown_statement"]
        reason_refs: tuple[str, ...] = () if assessment is None else (assessment.assessment_id,)
    elif assessment.assessment == Assessment.ACCEPTED:
        outcome = ConclusionOutcome.PASS
        statement = rule["pass_statement"]
        reason_refs = (assessment.assessment_id,)
    elif assessment.assessment == Assessment.CONTRADICTED:
        outcome = ConclusionOutcome.FAIL
        statement = rule["fail_statement"]
        reason_refs = (assessment.assessment_id,)
    else:
        outcome = ConclusionOutcome.CONFLICT
        statement = rule["conflict_statement"]
        reason_refs = (assessment.assessment_id,)
    return ConclusionRuleResult(
        outcome=outcome,
        statement=statement,
        dependency_claim_ids=(claim_id,),
        reason_refs=reason_refs,
    )
