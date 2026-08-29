from veritas.domain.enums import ChangeType, EdgeType
from veritas.domain.models import CandidateImpact, ChangeEvent, DependencyEdge, EvidenceSpan
from veritas.evidence.graph import EvidenceGraph
from veritas.storage.sqlite import SQLiteRepository


def propagate_change(
    repository: SQLiteRepository,
    event: ChangeEvent,
    *,
    new_edges: tuple[DependencyEdge, ...] = (),
    new_evidence: tuple[EvidenceSpan, ...] = (),
) -> CandidateImpact:
    graph = EvidenceGraph(repository)
    if event.change_type == ChangeType.CONFLICT:
        seed_claims = {
            edge.to_id
            for edge in new_edges
            if edge.edge_type in (EdgeType.SUPPORTS, EdgeType.CONTRADICTS)
        }
        return graph.candidate_impact_from_claims(
            seed_claims,
            evidence_ids=[evidence.evidence_id for evidence in new_evidence],
        )
    return graph.candidate_impact(event)

