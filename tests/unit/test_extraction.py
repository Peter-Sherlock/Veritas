from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from veritas.domain.enums import EdgeType
from veritas.extraction.models import ExtractionContractError
from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
    extract_document,
)
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.search.provider import SearchResult, VersionedDocument


QUESTION = "What retry behavior is documented?"
REASONED_AT = "2026-08-29T00:00:00Z"


def _document(
    *,
    doc_id: str = "retry",
    content: str = "HTTPX retries connection setup failures.",
) -> VersionedDocument:
    return VersionedDocument(
        doc_id=doc_id,
        version_id="1.0",
        title="Retry",
        content=content,
        published_at="2026-08-01T00:00:00Z",
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _provider(
    document: VersionedDocument,
    payload: object,
    *,
    question: str = QUESTION,
) -> FixtureLLM:
    prompt = build_extraction_prompt(question, document)
    return FixtureLLM(
        {
            fixture_key(EXTRACTION_SYSTEM_PROMPT, prompt): (
                payload if isinstance(payload, str) else json.dumps(payload)
            )
        }
    )


class _SingleDocumentSearch:
    def __init__(self, document: VersionedDocument) -> None:
        self.document = document

    def search(self, query: str, *, top_k: int = 5, as_of: str | None = None):
        return [
            SearchResult(
                doc_id=self.document.doc_id,
                version_id=self.document.version_id,
                title=self.document.title,
                path=Path("fixture.md"),
                score=1.0,
                snippet=self.document.content,
            )
        ][:top_k]

    def fetch(self, doc_id: str, version_id: str) -> VersionedDocument:
        if (doc_id, version_id) != (self.document.doc_id, self.document.version_id):
            raise KeyError((doc_id, version_id))
        return self.document


class ExtractionContractTests(unittest.TestCase):
    def test_exact_quote_is_aligned(self) -> None:
        document = _document()
        payload = {
            "assertions": [
                {
                    "statement": "HTTPX retries connection setup failures",
                    "canonical_key": "httpx.retries.connection_setup=true",
                    "relation": "supports",
                    "quote": document.content,
                }
            ]
        }
        result = extract_document(
            _provider(document, payload),
            question=QUESTION,
            document=document,
        )
        assertion = result.assertions[0]
        self.assertEqual(0, assertion.char_start)
        self.assertEqual(len(document.content), assertion.char_end)

    def test_invalid_json_is_classified(self) -> None:
        document = _document()
        with self.assertRaises(ExtractionContractError) as caught:
            extract_document(
                _provider(document, "not-json"),
                question=QUESTION,
                document=document,
            )
        self.assertEqual("invalid_json", caught.exception.code)

    def test_schema_relation_and_key_failures_are_classified(self) -> None:
        document = _document()
        cases = [
            (
                {"assertions": [], "commentary": "extra"},
                "invalid_schema",
            ),
            (
                {
                    "assertions": [
                        {
                            "statement": "A",
                            "canonical_key": "valid.key",
                            "relation": "maybe",
                            "quote": document.content,
                        }
                    ]
                },
                "invalid_relation",
            ),
            (
                {
                    "assertions": [
                        {
                            "statement": "A",
                            "canonical_key": "Invalid Key",
                            "relation": "supports",
                            "quote": document.content,
                        }
                    ]
                },
                "invalid_canonical_key",
            ),
        ]
        for payload, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ExtractionContractError) as caught:
                    extract_document(
                        _provider(document, payload),
                        question=QUESTION,
                        document=document,
                    )
                self.assertEqual(expected_code, caught.exception.code)

    def test_missing_and_ambiguous_citations_are_rejected(self) -> None:
        cases = [
            (_document(content="one exact sentence"), "missing", "citation_not_found"),
            (_document(content="repeat repeat"), "repeat", "citation_ambiguous"),
        ]
        for document, quote, expected_code in cases:
            payload = {
                "assertions": [
                    {
                        "statement": "A",
                        "canonical_key": "valid.key",
                        "relation": "supports",
                        "quote": quote,
                    }
                ]
            }
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ExtractionContractError) as caught:
                    extract_document(
                        _provider(document, payload),
                        question=QUESTION,
                        document=document,
                    )
                self.assertEqual(expected_code, caught.exception.code)


class ExtractionPipelineTests(unittest.TestCase):
    def test_materializes_stable_domain_candidates(self) -> None:
        document = _document()
        payload = {
            "assertions": [
                {
                    "statement": "HTTPX retries connection setup failures",
                    "canonical_key": "httpx.retries.connection_setup=true",
                    "relation": "supports",
                    "quote": document.content,
                }
            ]
        }
        pipeline = ResearchExtractionPipeline(
            _SingleDocumentSearch(document),
            _provider(document, payload),
            source_namespace="fixture-corpus",
        )
        first = pipeline.run(
            query="retry",
            question=QUESTION,
            reasoned_at=REASONED_AT,
            top_k=1,
        )
        second = pipeline.run(
            query="retry",
            question=QUESTION,
            reasoned_at=REASONED_AT,
            top_k=1,
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(1, len(first.evidence_spans))
        self.assertEqual(1, len(first.claims))
        self.assertEqual(1, len(first.edges))
        self.assertEqual(EdgeType.SUPPORTS, first.edges[0].edge_type)
        self.assertEqual(first.evidence_spans[0].evidence_id, first.edges[0].from_id)
        self.assertEqual(first.claims[0].claim_id, first.edges[0].to_id)
        self.assertEqual(
            "fixture-corpus:retry@1.0",
            first.evidence_spans[0].source_version_id,
        )

    def test_contradiction_materializes_contradicts_edge(self) -> None:
        document = _document()
        payload = {
            "assertions": [
                {
                    "statement": "HTTPX never retries connection setup failures",
                    "canonical_key": "httpx.retries.connection_setup=true",
                    "relation": "contradicts",
                    "quote": document.content,
                }
            ]
        }
        bundle = ResearchExtractionPipeline(
            _SingleDocumentSearch(document),
            _provider(document, payload),
            source_namespace="fixture-corpus",
        ).run(
            query="retry",
            question=QUESTION,
            reasoned_at=REASONED_AT,
            top_k=1,
        )
        self.assertEqual(EdgeType.CONTRADICTS, bundle.edges[0].edge_type)

    def test_empty_extraction_produces_no_candidates(self) -> None:
        document = _document()
        bundle = ResearchExtractionPipeline(
            _SingleDocumentSearch(document),
            _provider(document, {"assertions": []}),
            source_namespace="fixture-corpus",
        ).run(
            query="retry",
            question=QUESTION,
            reasoned_at=REASONED_AT,
            top_k=1,
        )
        self.assertEqual((), bundle.evidence_spans)
        self.assertEqual((), bundle.claims)
        self.assertEqual((), bundle.edges)


if __name__ == "__main__":
    unittest.main()
