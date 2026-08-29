"""Search provider protocol and local versioned-corpus implementation.

The first search adapter is a local, version-locked corpus so that
benchmarks stay reproducible (evaluation before integration, D-002).
"""

from veritas.search.local_corpus import LocalCorpusProvider
from veritas.search.provider import SearchProvider, SearchResult, VersionedDocument

__all__ = ["LocalCorpusProvider", "SearchProvider", "SearchResult", "VersionedDocument"]
