"""Versioned web sources: fetch, canonicalize and ledger content (M4-1, D-048).

The adapter generalizes the frozen corpus contract (M1-1R, D-024) to
re-fetchable web sources:

* Content is canonicalized exactly like corpus snapshots — strict UTF-8
  decode with CR/CRLF normalized to LF — and hashed over the canonical
  bytes. Hash what you keep, keep what you hash.
* Every accepted fetch appends an immutable ledger row. A source's
  version timeline is its sequence of distinct content states in fetch
  order: re-fetching unchanged content is a SAME observation, not a new
  version (the M1-5B SAME-step semantics carried over to the web). A
  revert is a new version — the timeline position changed even though
  the hash re-appeared.
* Version labels are local observation ordinals (``f1``, ``f2``, ...),
  not upstream tags — the web has no version labels. Veritas pins *when
  it first saw* each distinct content state (``published_at``) and *how
  many distinct states* it has seen (the label).
* ``materialize_corpus`` projects the ledger into the frozen corpus
  layout (``manifest.json`` + per-version snapshot files), so the graph
  bridge, the extraction pipeline, retrieval and revision events consume
  fetched sources with zero changes downstream.
* ``detect_web_drift`` (in :mod:`veritas.autonomy.watch`) compares the
  evolution store's active web sources against the ledger's latest
  version — drift is measured against the last observation, not a
  static manifest.

The fetch shell is the only network code; the ledger, the canonicalizer
and the materializer are pure and fully replayable with an injected
transport. Content is attributed to the requested URL — HTTP redirects
are followed for retrieval but do not rewrite provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


WEB_SOURCES_SCHEMA = "web-sources-1"

Transport = Callable[[str], tuple[int, bytes]]


class WebSourceError(ValueError):
    """A stable, classifiable failure at the versioned web-source boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical_web_text(body: bytes) -> str:
    """Normalize a response body under the corpus content contract.

    Strict UTF-8 decode, CR/CRLF folded to LF. Empty or whitespace-only
    bodies are rejected: a source that carries no text cannot anchor
    evidence, and silently keeping it would version nothing.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSourceError(
            "non_utf8_body",
            f"response body is not valid UTF-8 at byte {exc.start}",
        ) from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise WebSourceError("empty_web_body", "response body carries no text")
    return normalized


def _require_http_url(url: str) -> str:
    if not url or not url.lower().startswith(("http://", "https://")):
        raise WebSourceError(
            "invalid_web_url",
            f"url {url!r} must be an absolute http(s) URL",
        )
    return url


_SLUG_CLEAN = re.compile(r"[^a-z0-9._-]+")


def url_slug(url: str) -> str:
    """Stable, filesystem-safe doc_id for a URL.

    The sanitized address keeps the slug human-readable; the truncated
    URL hash keeps it collision-free. The slug appears in source ids,
    manifest paths and change-event ids, so it must never contain a
    character that breaks filenames on any supported platform.
    """
    _require_http_url(url)
    address = url.split("://", 1)[1].lower()
    cleaned = _SLUG_CLEAN.sub("-", address).strip("-.") or "source"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


@dataclass(frozen=True)
class WebVersion:
    """One distinct content state of a web source, in first-seen order."""

    url: str
    version_label: str
    content_hash: str
    published_at: str  # instant this content state was first observed
    content: str


@dataclass(frozen=True)
class WebFetchOutcome:
    """The ledger's decision about one fetch."""

    url: str
    fetched_at: str
    doc_slug: str
    content_hash: str
    version_label: str
    status: str  # "new_version" | "same_content"


class WebSourceStore:
    """Append-only fetch ledger for versioned web sources (schema web-sources-1).

    Rows are keyed by (url, fetched_at) and immutable: re-recording the
    exact same fetch is a no-op returning the stored outcome (replay
    safety — a re-run must reproduce the original decision byte for
    byte), while the same key with different content is a
    ``web_fetch_conflict`` — one observation instant cannot hold two
    payloads.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        try:
            self._initialize_schema()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "WebSourceStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS web_fetches (
                url TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                version_label TEXT NOT NULL,
                status TEXT NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (url, fetched_at)
            );
            """
        )
        row = self.connection.execute(
            "SELECT value FROM store_meta WHERE key = 'schema'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO store_meta (key, value) VALUES ('schema', ?)",
                (WEB_SOURCES_SCHEMA,),
            )
            return
        stored_schema = str(row["value"])
        if stored_schema != WEB_SOURCES_SCHEMA:
            raise WebSourceError(
                "web_store_schema_drift",
                f"store holds schema {stored_schema!r}, expected "
                f"{WEB_SOURCES_SCHEMA!r}",
            )

    def record_fetch(
        self, url: str, content: str, *, fetched_at: str
    ) -> WebFetchOutcome:
        """Append one fetch observation; derive or reuse its version identity."""
        _require_http_url(url)
        if not content.strip():
            raise WebSourceError("empty_web_body", "recorded content carries no text")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        slug = url_slug(url)
        with self.transaction():
            existing = self.connection.execute(
                "SELECT content_hash, content, version_label, status "
                "FROM web_fetches WHERE url = ? AND fetched_at = ?",
                (url, fetched_at),
            ).fetchone()
            if existing is not None:
                if str(existing["content_hash"]) != content_hash or str(
                    existing["content"]
                ) != content:
                    raise WebSourceError(
                        "web_fetch_conflict",
                        f"{url!r} was already recorded at {fetched_at!r} with "
                        "different content; one observation instant cannot "
                        "hold two payloads",
                    )
                # Replay of a recorded fetch: reproduce the stored outcome.
                return WebFetchOutcome(
                    url=url,
                    fetched_at=fetched_at,
                    doc_slug=slug,
                    content_hash=content_hash,
                    version_label=str(existing["version_label"]),
                    status=str(existing["status"]),
                )

            prior_versions = self.versions(url)
            if prior_versions and prior_versions[-1].content_hash == content_hash:
                status = "same_content"
                label = prior_versions[-1].version_label
            else:
                status = "new_version"
                label = f"f{len(prior_versions) + 1}"
            self.connection.execute(
                "INSERT INTO web_fetches (url, fetched_at, content_hash, "
                "version_label, status, content) VALUES (?, ?, ?, ?, ?, ?)",
                (url, fetched_at, content_hash, label, status, content),
            )
        return WebFetchOutcome(
            url=url,
            fetched_at=fetched_at,
            doc_slug=slug,
            content_hash=content_hash,
            version_label=label,
            status=status,
        )

    def versions(self, url: str) -> list[WebVersion]:
        """Distinct content states of a source, in first-seen order.

        A new version is a *label change* in the fetch sequence, not a
        hash change — a revert re-introduces an old hash as a genuinely
        new timeline state, while SAME fetches repeat the current label
        and contribute nothing.
        """
        rows = self.connection.execute(
            "SELECT url, fetched_at, content_hash, version_label, content "
            "FROM web_fetches WHERE url = ? ORDER BY fetched_at",
            (url,),
        ).fetchall()
        result: list[WebVersion] = []
        current_label: str | None = None
        for row in rows:
            label = str(row["version_label"])
            if label == current_label:
                continue
            current_label = label
            result.append(
                WebVersion(
                    url=str(row["url"]),
                    version_label=label,
                    content_hash=str(row["content_hash"]),
                    published_at=str(row["fetched_at"]),
                    content=str(row["content"]),
                )
            )
        return result

    def latest(self, url: str) -> WebVersion:
        """The source's most recent distinct content state."""
        versions = self.versions(url)
        if not versions:
            raise KeyError(f"no fetch recorded for {url!r}")
        return versions[-1]

    def last_fetch_at(self, url: str) -> str | None:
        """Instant of the most recent fetch observation (SAME or not)."""
        row = self.connection.execute(
            "SELECT MAX(fetched_at) AS last_at FROM web_fetches WHERE url = ?",
            (url,),
        ).fetchone()
        return None if row is None or row["last_at"] is None else str(row["last_at"])

    def urls(self) -> list[str]:
        return sorted(
            str(row["url"])
            for row in self.connection.execute(
                "SELECT DISTINCT url FROM web_fetches"
            )
        )

    def slug_index(self) -> dict[str, str]:
        """Reverse mapping doc_slug -> url for drift detection."""
        index: dict[str, str] = {}
        for url in self.urls():
            slug = url_slug(url)
            if slug in index:
                raise WebSourceError(
                    "web_slug_collision",
                    f"slugs of {url!r} and {index[slug]!r} collide",
                )
            index[slug] = url
        return index

    def counts(self) -> dict[str, int]:
        return {
            "urls": len(self.urls()),
            "fetches": int(
                self.connection.execute("SELECT COUNT(*) FROM web_fetches").fetchone()[0]
            ),
            "versions": sum(len(self.versions(url)) for url in self.urls()),
        }


def _urllib_transport(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
    """Default network transport: a plain HTTP GET."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "veritas-web-sources/1"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read()


def fetch_web_source(
    store: WebSourceStore,
    url: str,
    *,
    observed_at: str,
    transport: Transport | None = None,
    max_bytes: int = 2_000_000,
) -> WebFetchOutcome:
    """Fetch one URL through a transport and record it in the ledger.

    Network failures map to typed errors and never touch the ledger; a
    body larger than ``max_bytes`` is rejected before it can dominate
    the store. ``observed_at`` is the caller-supplied observation
    instant so live runs stay replayable (the transport is the only
    nondeterminism, and it is injectable).
    """
    _require_http_url(url)
    transport = transport or _urllib_transport
    try:
        status, body = transport(url)
    except urllib.error.HTTPError as exc:
        # HTTPError wraps the live error response; closing it releases
        # the socket before the typed error replaces it.
        exc.close()
        raise WebSourceError(
            "web_fetch_http_error",
            f"fetching {url!r} returned HTTP {exc.code}",
        ) from exc
    except Exception as exc:
        raise WebSourceError(
            "web_fetch_unreachable",
            f"fetching {url!r} failed: {exc}",
        ) from exc
    if status != 200:
        raise WebSourceError(
            "web_fetch_http_error",
            f"fetching {url!r} returned HTTP {status}",
        )
    if len(body) > max_bytes:
        raise WebSourceError(
            "web_body_too_large",
            f"response from {url!r} is {len(body)} bytes, over the "
            f"{max_bytes}-byte limit",
        )
    return store.record_fetch(url, canonical_web_text(body), fetched_at=observed_at)


def materialize_corpus(
    store: WebSourceStore, root: str | Path, *, corpus_id: str
) -> Path:
    """Project the ledger into the frozen corpus layout.

    Deterministic by construction: sources sorted by URL, versions in
    first-seen order, canonical content written once with LF endings.
    Re-materializing the same ledger state reproduces byte-identical
    files, and the result loads through ``LocalCorpusProvider`` with
    hash verification on — the ledger is the source of truth, the
    directory is a derived view.
    """
    if not corpus_id.strip():
        raise WebSourceError("invalid_corpus_id", "corpus_id must be a non-empty string")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, object]] = []
    for url in store.urls():
        slug = url_slug(url)
        doc_dir = root / slug
        doc_dir.mkdir(exist_ok=True)
        versions: list[dict[str, object]] = []
        for version in store.versions(url):
            filename = f"{version.version_label}.md"
            (doc_dir / filename).write_text(
                version.content, encoding="utf-8", newline="\n"
            )
            versions.append(
                {
                    "version_id": version.version_label,
                    "path": f"{slug}/{filename}",
                    "content_hash": version.content_hash,
                    "published_at": version.published_at,
                }
            )
        documents.append({"doc_id": slug, "title": url, "versions": versions})
    manifest = {"corpus_id": corpus_id, "documents": documents}
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return root
