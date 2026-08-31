from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from veritas.domain.enums import EdgeType
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionCandidateBundle":
        """Rehydrate the canonical representation stored in the runtime outbox."""
        return cls(
            query=str(data["query"]),
            question=str(data["question"]),
            retrieved=tuple(
                SearchResult(
                    doc_id=str(result["doc_id"]),
                    version_id=str(result["version_id"]),
                    title=str(result["title"]),
                    path=Path(result["path"]),
                    score=float(result["score"]),
                    snippet=str(result["snippet"]),
                )
                for result in data["retrieved"]
            ),
            documents=tuple(
                ExtractionDocumentResult(
                    doc_id=str(document["doc_id"]),
                    version_id=str(document["version_id"]),
                    model_id=str(document["model_id"]),
                    prompt_version=str(document["prompt_version"]),
                    schema_version=str(document["schema_version"]),
                    prompt_tokens=int(document["prompt_tokens"]),
                    completion_tokens=int(document["completion_tokens"]),
                    assertions=tuple(
                        ExtractedAssertion(
                            statement=str(assertion["statement"]),
                            canonical_key=str(assertion["canonical_key"]),
                            relation=str(assertion["relation"]),
                            quote=str(assertion["quote"]),
                            char_start=int(assertion["char_start"]),
                            char_end=int(assertion["char_end"]),
                        )
                        for assertion in document["assertions"]
                    ),
                )
                for document in data["documents"]
            ),
            evidence_spans=tuple(
                EvidenceSpan(
                    evidence_id=str(span["evidence_id"]),
                    source_version_id=str(span["source_version_id"]),
                    locator=dict(span["locator"]),
                    text=str(span["text"]),
                    text_hash=str(span["text_hash"]),
                    normalized_assertion=str(span["normalized_assertion"]),
                    valid_from=str(span["valid_from"]),
                    valid_to=(
                        None if span["valid_to"] is None else str(span["valid_to"])
                    ),
                )
                for span in data["evidence_spans"]
            ),
            claims=tuple(
                Claim(
                    claim_id=str(claim["claim_id"]),
                    statement=str(claim["statement"]),
                    created_at=str(claim["created_at"]),
                    canonical_key=str(claim["canonical_key"]),
                )
                for claim in data["claims"]
            ),
            edges=tuple(
                DependencyEdge(
                    edge_id=str(edge["edge_id"]),
                    edge_type=EdgeType(edge["edge_type"]),
                    from_id=str(edge["from_id"]),
                    to_id=str(edge["to_id"]),
                    created_at=str(edge["created_at"]),
                    valid_from=str(edge["valid_from"]),
                    valid_to=(
                        None if edge["valid_to"] is None else str(edge["valid_to"])
                    ),
                    rule_version=str(edge["rule_version"]),
                )
                for edge in data["edges"]
            ),
        )
