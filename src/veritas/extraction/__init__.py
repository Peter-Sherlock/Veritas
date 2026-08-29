"""Strict, source-grounded extraction for initial research.

Probabilistic providers may propose assertions and verbatim quotes. This
package validates those proposals and deterministically materializes domain
candidates; providers never assign lineage identifiers.
"""

from veritas.extraction.models import (
    ExtractedAssertion,
    ExtractionCandidateBundle,
    ExtractionContractError,
    ExtractionDocumentResult,
)
from veritas.extraction.pipeline import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
    extract_document,
)

__all__ = [
    "EXTRACTION_PROMPT_VERSION",
    "EXTRACTION_SCHEMA_VERSION",
    "EXTRACTION_SYSTEM_PROMPT",
    "ExtractedAssertion",
    "ExtractionCandidateBundle",
    "ExtractionContractError",
    "ExtractionDocumentResult",
    "ResearchExtractionPipeline",
    "build_extraction_prompt",
    "extract_document",
]
