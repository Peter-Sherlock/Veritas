from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from veritas.domain.models import Claim, DependencyEdge, EvidenceSpan, json_ready
from veritas.search.provider import SearchResult


class ExtractionContractError(ValueError):
    """A stable, classifiable failure at the probabilistic boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ExtractedAssertion:
    statement: str
    canonical_key: str
    relation: str
    quote: str
    char_start: int
    char_end: int

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class ExtractionDocumentResult:
    doc_id: str
    version_id: str
    model_id: str
    prompt_version: str
    schema_version: str
    prompt_tokens: int
    completion_tokens: int
    assertions: tuple[ExtractedAssertion, ...]

    def to_dict(self) -> dict[str, Any]:
        return json_ready(asdict(self))


@dataclass(frozen=True)
class ExtractionCandidateBundle:
    query: str
    question: str
    retrieved: tuple[SearchResult, ...]
    documents: tuple[ExtractionDocumentResult, ...]
    evidence_spans: tuple[EvidenceSpan, ...]
    claims: tuple[Claim, ...]
    edges: tuple[DependencyEdge, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "question": self.question,
            "retrieved": [
                {
                    "doc_id": result.doc_id,
                    "version_id": result.version_id,
                    "title": result.title,
                    "path": result.path.as_posix(),
                    "score": result.score,
                    "snippet": result.snippet,
                }
                for result in self.retrieved
            ],
            "documents": [document.to_dict() for document in self.documents],
            "evidence_spans": [span.to_dict() for span in self.evidence_spans],
            "claims": [claim.to_dict() for claim in self.claims],
            "edges": [edge.to_dict() for edge in self.edges],
        }
