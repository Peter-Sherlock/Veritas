"""Sources: versioned acquisition of research sources (M4)."""

from veritas.sources.web import (
    WEB_SOURCES_SCHEMA,
    Transport,
    WebFetchOutcome,
    WebSourceError,
    WebSourceStore,
    WebVersion,
    canonical_web_text,
    fetch_web_source,
    materialize_corpus,
    url_slug,
)

__all__ = [
    "WEB_SOURCES_SCHEMA",
    "Transport",
    "WebFetchOutcome",
    "WebSourceError",
    "WebSourceStore",
    "WebVersion",
    "canonical_web_text",
    "fetch_web_source",
    "materialize_corpus",
    "url_slug",
]
