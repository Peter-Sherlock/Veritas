"""LLM provider protocol and implementations.

Design rule (D-002, D-021): the LLM is a replaceable probabilistic
component. It only turns text into structured assertions; versioning,
idempotency and state transitions stay deterministic.
"""

from veritas.providers.llm import (
    FixtureLLM,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleClient,
    RecordingLLM,
    fixture_key,
)

__all__ = [
    "FixtureLLM",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleClient",
    "RecordingLLM",
    "fixture_key",
]
