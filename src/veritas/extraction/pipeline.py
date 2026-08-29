from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from veritas.domain.enums import EdgeType
from veritas.domain.models import Claim, DependencyEdge, EvidenceSpan
from veritas.extraction.models import (
    ExtractedAssertion,
    ExtractionCandidateBundle,
    ExtractionContractError,
    ExtractionDocumentResult,
)
from veritas.providers.llm import LLMProvider
from veritas.search.provider import SearchProvider, VersionedDocument


EXTRACTION_SCHEMA_VERSION = "evidence-assertion-1"
EXTRACTION_PROMPT_VERSION = "httpx-extractor-1"
EXTRACTION_SYSTEM_PROMPT = """You extract source-grounded technical assertions.
Return one JSON object with exactly one key named "assertions".
Each assertion must have exactly: statement, canonical_key, relation, quote.
relation must be "supports" or "contradicts".
quote must be a verbatim, uniquely occurring substring of the supplied document.
Do not answer from prior knowledge. Return an empty assertions list when the
document does not contain evidence that answers the question."""

_CANONICAL_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:=/-]*$")
_ASSERTION_KEYS = {"statement", "canonical_key", "relation", "quote"}
_RELATIONS = {"supports", "contradicts"}


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def build_extraction_prompt(question: str, document: VersionedDocument) -> str:
    if not question or not question.strip():
        raise ValueError("question must not be empty")
    payload = {
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "question": question,
        "document": {
            "doc_id": document.doc_id,
            "version_id": document.version_id,
            "title": document.title,
            "content": document.content,
            "content_hash": document.content_hash,
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionContractError(
            "invalid_schema",
            f"{field_name} must be a non-empty string",
        )
    return value


def _parse_assertions(text: str, document: VersionedDocument) -> tuple[ExtractedAssertion, ...]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExtractionContractError(
            "invalid_json",
            "provider response is not valid JSON",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"assertions"}:
        raise ExtractionContractError(
            "invalid_schema",
            'top-level object must contain exactly the "assertions" key',
        )
    raw_assertions = payload["assertions"]
    if not isinstance(raw_assertions, list):
        raise ExtractionContractError("invalid_schema", "assertions must be a list")
    if len(raw_assertions) > 20:
        raise ExtractionContractError("invalid_schema", "at most 20 assertions are allowed")

    assertions: list[ExtractedAssertion] = []
    identities: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_assertions):
        if not isinstance(raw, dict) or set(raw) != _ASSERTION_KEYS:
            raise ExtractionContractError(
                "invalid_schema",
                f"assertions[{index}] must contain exactly {sorted(_ASSERTION_KEYS)}",
            )
        statement = _require_string(raw["statement"], f"assertions[{index}].statement")
        canonical_key = _require_string(
            raw["canonical_key"], f"assertions[{index}].canonical_key"
        )
        relation = _require_string(raw["relation"], f"assertions[{index}].relation")
        quote = _require_string(raw["quote"], f"assertions[{index}].quote")
        if not _CANONICAL_KEY_PATTERN.fullmatch(canonical_key):
            raise ExtractionContractError(
                "invalid_canonical_key",
                f"assertions[{index}].canonical_key has unsupported characters",
            )
        if relation not in _RELATIONS:
            raise ExtractionContractError(
                "invalid_relation",
                f"assertions[{index}].relation must be supports or contradicts",
            )
        occurrences = document.content.count(quote)
        if occurrences == 0:
            raise ExtractionContractError(
                "citation_not_found",
                f"assertions[{index}].quote is not an exact document substring",
            )
        if occurrences != 1:
            raise ExtractionContractError(
                "citation_ambiguous",
                f"assertions[{index}].quote occurs {occurrences} times",
            )
        char_start = document.content.index(quote)
        identity = (canonical_key, relation, quote)
        if identity in identities:
            raise ExtractionContractError(
                "duplicate_assertion",
                f"assertions[{index}] duplicates an earlier assertion",
            )
        identities.add(identity)
        assertions.append(
            ExtractedAssertion(
                statement=statement,
                canonical_key=canonical_key,
                relation=relation,
                quote=quote,
                char_start=char_start,
                char_end=char_start + len(quote),
            )
        )
    return tuple(assertions)


def extract_document(
    provider: LLMProvider,
    *,
    question: str,
    document: VersionedDocument,
) -> ExtractionDocumentResult:
    prompt = build_extraction_prompt(question, document)
    response = provider.complete(
        system=EXTRACTION_SYSTEM_PROMPT,
        prompt=prompt,
        json_mode=True,
    )
    assertions = _parse_assertions(response.text, document)
    return ExtractionDocumentResult(
        doc_id=document.doc_id,
        version_id=document.version_id,
        model_id=response.model_id,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        schema_version=EXTRACTION_SCHEMA_VERSION,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        assertions=assertions,
    )


class ResearchExtractionPipeline:
    """Search, fetch, validate extraction, then materialize domain candidates."""

    def __init__(
        self,
        search: SearchProvider,
        provider: LLMProvider,
        *,
        source_namespace: str,
    ) -> None:
        if not source_namespace or not source_namespace.strip():
            raise ValueError("source_namespace must not be empty")
        self._search = search
        self._provider = provider
        self._source_namespace = source_namespace

    def run(
        self,
        *,
        query: str,
        question: str,
        reasoned_at: str,
        top_k: int = 3,
        as_of: str | None = None,
    ) -> ExtractionCandidateBundle:
        retrieved = tuple(self._search.search(query, top_k=top_k, as_of=as_of))
        documents: list[ExtractionDocumentResult] = []
        source_documents: dict[tuple[str, str], VersionedDocument] = {}
        for result in retrieved:
            document = self._search.fetch(result.doc_id, result.version_id)
            source_documents[(result.doc_id, result.version_id)] = document
            documents.append(
                extract_document(
                    self._provider,
                    question=question,
                    document=document,
                )
            )

        evidence_spans: list[EvidenceSpan] = []
        claims_by_key: dict[str, Claim] = {}
        edges: list[DependencyEdge] = []
        for result in documents:
            document = source_documents[(result.doc_id, result.version_id)]
            source_version_id = (
                f"{self._source_namespace}:{result.doc_id}@{result.version_id}"
            )
            valid_from = document.published_at or reasoned_at
            for assertion in result.assertions:
                existing = claims_by_key.get(assertion.canonical_key)
                if existing is not None and existing.statement != assertion.statement:
                    raise ExtractionContractError(
                        "canonical_key_conflict",
                        f"{assertion.canonical_key} maps to conflicting statements",
                    )
                if existing is None:
                    existing = Claim(
                        claim_id=_stable_id("claim", assertion.canonical_key),
                        statement=assertion.statement,
                        created_at=reasoned_at,
                        canonical_key=assertion.canonical_key,
                    )
                    claims_by_key[assertion.canonical_key] = existing

                evidence = EvidenceSpan(
                    evidence_id=_stable_id(
                        "evidence",
                        source_version_id,
                        assertion.quote,
                        assertion.canonical_key,
                        assertion.relation,
                    ),
                    source_version_id=source_version_id,
                    locator={
                        "doc_id": result.doc_id,
                        "version_id": result.version_id,
                        "char_start": assertion.char_start,
                        "char_end": assertion.char_end,
                    },
                    text=assertion.quote,
                    text_hash=hashlib.sha256(assertion.quote.encode("utf-8")).hexdigest(),
                    normalized_assertion=assertion.statement,
                    valid_from=valid_from,
                )
                evidence_spans.append(evidence)
                edge_type = (
                    EdgeType.SUPPORTS
                    if assertion.relation == "supports"
                    else EdgeType.CONTRADICTS
                )
                edges.append(
                    DependencyEdge(
                        edge_id=_stable_id(
                            "edge",
                            evidence.evidence_id,
                            edge_type.value,
                            existing.claim_id,
                        ),
                        edge_type=edge_type,
                        from_id=evidence.evidence_id,
                        to_id=existing.claim_id,
                        created_at=reasoned_at,
                        valid_from=valid_from,
                        valid_to=None,
                        rule_version=EXTRACTION_PROMPT_VERSION,
                    )
                )

        return ExtractionCandidateBundle(
            query=query,
            question=question,
            retrieved=retrieved,
            documents=tuple(documents),
            evidence_spans=tuple(evidence_spans),
            claims=tuple(claims_by_key[key] for key in sorted(claims_by_key)),
            edges=tuple(edges),
        )
