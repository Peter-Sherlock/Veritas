from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SearchResult:
    doc_id: str
    version_id: str
    title: str
    path: Path
    score: float
    snippet: str


@dataclass(frozen=True)
class VersionedDocument:
    doc_id: str
    version_id: str
    title: str
    content: str
    published_at: str | None
    content_hash: str


class SearchProvider(Protocol):
    """Retrieval boundary. Implementations must be replaceable without
    touching research or evolution logic."""

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        as_of: str | None = None,
    ) -> list[SearchResult]: ...

    def fetch(self, doc_id: str, version_id: str) -> VersionedDocument: ...
