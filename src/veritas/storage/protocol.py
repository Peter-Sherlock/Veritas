from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

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


class EvidenceRepository(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...

    def find_evolution_run(self, project_id: str, external_event_id: str) -> EvolutionRun | None: ...

    def insert_source_version(self, source: SourceVersion) -> None: ...

    def insert_evidence_span(self, evidence: EvidenceSpan) -> None: ...

    def insert_claim(self, claim: Claim) -> None: ...

    def insert_claim_assessment(self, assessment: ClaimAssessment) -> None: ...

    def insert_conclusion_version(self, conclusion: ConclusionVersion) -> None: ...

    def insert_dependency_edge(self, edge: DependencyEdge) -> None: ...

    def insert_change_event(self, event: ChangeEvent) -> None: ...

    def insert_evolution_run(self, run: EvolutionRun) -> None: ...

