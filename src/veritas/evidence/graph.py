from __future__ import annotations

from collections import defaultdict, deque

from veritas.domain.enums import EdgeType
from veritas.domain.models import CandidateImpact, ChangeEvent
from veritas.storage.sqlite import SQLiteRepository


class EvidenceGraph:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def candidate_impact(self, event: ChangeEvent) -> CandidateImpact:
        """Walk the pre-change graph and return nodes requiring reverification."""
        old_evidence = self.repository.list_evidence_for_source(event.old_source_version_id)
        if event.changed_locators:
            locator_set = {self._locator_key(locator) for locator in event.changed_locators}
            old_evidence = [
                evidence
                for evidence in old_evidence
                if self._locator_key(evidence.locator) in locator_set
            ]

        evidence_ids = {evidence.evidence_id for evidence in old_evidence}
        claim_ids = {claim.claim_id for claim in self.repository.list_claims()}
        current_conclusions = {
            conclusion.conclusion_version_id: conclusion.conclusion_key
            for conclusion in self.repository.list_current_conclusions()
        }

        traversable_types = {
            EdgeType.SUPPORTS,
            EdgeType.CONTRADICTS,
            EdgeType.DEPENDS_ON,
        }
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in self.repository.list_dependency_edges():
            if edge.edge_type in traversable_types:
                adjacency[edge.from_id].append(edge.to_id)
        for targets in adjacency.values():
            targets.sort()

        queue = deque(sorted(evidence_ids))
        visited = set(evidence_ids)
        impacted_claims: set[str] = set()
        impacted_conclusions: set[str] = set()

        while queue:
            node_id = queue.popleft()
            for target_id in adjacency.get(node_id, []):
                if target_id in visited:
                    continue
                visited.add(target_id)
                queue.append(target_id)
                if target_id in claim_ids:
                    impacted_claims.add(target_id)
                if target_id in current_conclusions:
                    impacted_conclusions.add(current_conclusions[target_id])

        return CandidateImpact(
            evidence_spans=tuple(sorted(evidence_ids)),
            claims=tuple(sorted(impacted_claims)),
            conclusions=tuple(sorted(impacted_conclusions)),
        )

    @staticmethod
    def _locator_key(locator: dict[str, object]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((key, str(value)) for key, value in locator.items()))

