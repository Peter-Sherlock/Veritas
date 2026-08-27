from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .enums import Assessment, ChangeType, ConclusionOutcome, EdgeType


def _require_nonempty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_iso_datetime(value: str | None, field_name: str) -> None:
    if value is None:
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise ValueError(f"{field_name} must be a hexadecimal SHA-256 digest")


def json_ready(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


@dataclass(frozen=True)
class SourceVersion:
    source_id: str
    version_id: str
    version_label: str
    canonical_uri: str
    content_hash: str
    published_at: str | None
    observed_at: str
    valid_from: str
    valid_to: str | None = None
    supersedes_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_id", "version_id", "version_label", "canonical_uri"):
            _require_nonempty(getattr(self, name), name)
        _require_sha256(self.content_hash, "content_hash")
        for name in ("published_at", "observed_at", "valid_from", "valid_to"):
            _require_iso_datetime(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class EvidenceSpan:
    evidence_id: str
    source_version_id: str
    locator: dict[str, Any]
    text: str
    text_hash: str
    normalized_assertion: str
    valid_from: str
    valid_to: str | None = None

    def __post_init__(self) -> None:
        for name in ("evidence_id", "source_version_id", "text", "normalized_assertion"):
            _require_nonempty(getattr(self, name), name)
        _require_sha256(self.text_hash, "text_hash")
        _require_iso_datetime(self.valid_from, "valid_from")
        _require_iso_datetime(self.valid_to, "valid_to")

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class Claim:
    claim_id: str
    statement: str
    created_at: str
    canonical_key: str

    def __post_init__(self) -> None:
        for name in ("claim_id", "statement", "canonical_key"):
            _require_nonempty(getattr(self, name), name)
        _require_iso_datetime(self.created_at, "created_at")

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class ClaimAssessment:
    assessment_id: str
    claim_id: str
    snapshot_id: str
    assessment: Assessment
    rule_version: str
    reason_refs: tuple[str, ...]
    reasoned_at: str
    supersedes_assessment_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("assessment_id", "claim_id", "snapshot_id", "rule_version"):
            _require_nonempty(getattr(self, name), name)
        _require_iso_datetime(self.reasoned_at, "reasoned_at")

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class ConclusionVersion:
    conclusion_key: str
    conclusion_version_id: str
    version_number: int
    statement: str
    outcome: ConclusionOutcome
    dependency_rule: dict[str, Any]
    reason_refs: tuple[str, ...]
    reasoned_at: str
    supersedes_conclusion_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("conclusion_key", "conclusion_version_id", "statement"):
            _require_nonempty(getattr(self, name), name)
        if self.version_number < 1:
            raise ValueError("version_number must be at least 1")
        _require_iso_datetime(self.reasoned_at, "reasoned_at")

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class DependencyEdge:
    edge_id: str
    edge_type: EdgeType
    from_id: str
    to_id: str
    created_at: str
    valid_from: str
    valid_to: str | None
    rule_version: str

    def __post_init__(self) -> None:
        for name in ("edge_id", "from_id", "to_id", "rule_version"):
            _require_nonempty(getattr(self, name), name)
        for name in ("created_at", "valid_from", "valid_to"):
            _require_iso_datetime(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class ChangeEvent:
    change_event_id: str
    external_event_id: str
    project_id: str
    change_type: ChangeType
    old_source_version_id: str
    new_source_version_id: str | None
    changed_locators: tuple[dict[str, Any], ...]
    observed_at: str
    effective_at: str

    def __post_init__(self) -> None:
        for name in ("change_event_id", "external_event_id", "project_id", "old_source_version_id"):
            _require_nonempty(getattr(self, name), name)
        _require_iso_datetime(self.observed_at, "observed_at")
        _require_iso_datetime(self.effective_at, "effective_at")

    @property
    def idempotency_key(self) -> tuple[str, str]:
        return self.project_id, self.external_event_id

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class CandidateImpact:
    evidence_spans: tuple[str, ...]
    claims: tuple[str, ...]
    conclusions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class EvolutionRun:
    run_id: str
    scenario_id: str
    scenario_version: str
    project_id: str
    external_event_id: str
    change_event_id: str
    input_snapshot_id: str
    input_snapshot_hash: str
    rule_version: str
    candidate_impact: CandidateImpact
    reverification_results: tuple[dict[str, Any], ...]
    rechecked_unchanged: tuple[str, ...]
    confirmed_invalidations: tuple[dict[str, Any], ...]
    created_claims: tuple[str, ...]
    created_claim_assessments: tuple[str, ...]
    created_conclusions: tuple[str, ...]
    recomputed_conclusions: tuple[str, ...]
    untouched_nodes: tuple[str, ...]
    conclusion_diffs: tuple[dict[str, Any], ...]
    trace_events: tuple[dict[str, Any], ...]
    operational_metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvolutionRun":
        copied = dict(data)
        copied["candidate_impact"] = CandidateImpact(
            evidence_spans=tuple(copied["candidate_impact"]["evidence_spans"]),
            claims=tuple(copied["candidate_impact"]["claims"]),
            conclusions=tuple(copied["candidate_impact"]["conclusions"]),
        )
        tuple_fields = (
            "reverification_results",
            "rechecked_unchanged",
            "confirmed_invalidations",
            "created_claims",
            "created_claim_assessments",
            "created_conclusions",
            "recomputed_conclusions",
            "untouched_nodes",
            "conclusion_diffs",
            "trace_events",
        )
        for field_name in tuple_fields:
            copied[field_name] = tuple(copied[field_name])
        return cls(**copied)

