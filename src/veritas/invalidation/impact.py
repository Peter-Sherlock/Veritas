from veritas.domain.models import CandidateImpact, ChangeEvent
from veritas.evidence.graph import EvidenceGraph
from veritas.storage.sqlite import SQLiteRepository


def propagate_change(
    repository: SQLiteRepository,
    event: ChangeEvent,
) -> CandidateImpact:
    return EvidenceGraph(repository).candidate_impact(event)

