from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from veritas.extraction.models import ExtractionDocumentResult
from veritas.extraction.pipeline import derive_canonical_key


CANDIDATE_STORE_SCHEMA = "extraction-candidates-1"
_RELATIONS = {"supports", "contradicts"}


class CandidateStoreError(ValueError):
    """A stable, classifiable failure at the candidate-store boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class CandidateRecord:
    """One contract-valid extraction candidate awaiting claim aggregation."""

    source_version_id: str
    doc_id: str
    version_id: str
    canonical_key: str
    statement: str
    relation: str
    quote: str
    char_start: int
    char_end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def candidate_content_hash(*, statement: str, relation: str, quote: str) -> str:
    """Content hash over the full candidate payload.

    The hash covers statement, relation and quote, so an exact repeat from
    any run collapses to one row while any content drift — a cosmetic
    rewording, a flipped relation, a different quote span — lands as a
    separate candidate under the same canonical key.
    """
    payload = json.dumps(
        {"statement": statement, "relation": relation, "quote": quote},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_id_for(*, source_version_id: str, canonical_key: str, content_hash: str) -> str:
    digest = hashlib.sha256(
        "\n".join((source_version_id, canonical_key, content_hash)).encode("utf-8")
    ).hexdigest()[:20]
    return f"cand:{digest}"


def candidates_from_document(
    document: ExtractionDocumentResult,
    *,
    source_namespace: str,
) -> tuple[CandidateRecord, ...]:
    source_version_id = f"{source_namespace}:{document.doc_id}@{document.version_id}"
    return tuple(
        CandidateRecord(
            source_version_id=source_version_id,
            doc_id=document.doc_id,
            version_id=document.version_id,
            canonical_key=assertion.canonical_key,
            statement=assertion.statement,
            relation=assertion.relation,
            quote=assertion.quote,
            char_start=assertion.char_start,
            char_end=assertion.char_end,
        )
        for assertion in document.assertions
    )


class CandidateStore:
    """Transactional SQLite store for extraction candidates (D-032).

    Candidates are immutable observations of a (source version, assertion)
    pair. Identity is ``(source_version_id, canonical_key, content_hash)``
    where the content hash covers statement, relation and quote, so an exact
    repeat from any run is idempotent while any content drift persists as a
    separate candidate. The store never merges or overwrites: cross-run
    disagreement is surfaced through queries, not suppressed. The full
    derived canonical key is stored as text — hashing it would destroy the
    grouping queries that expose paraphrase noise.
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

    def __enter__(self) -> "CandidateStore":
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

            CREATE TABLE IF NOT EXISTS extraction_candidates (
                candidate_id TEXT PRIMARY KEY,
                source_version_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                statement TEXT NOT NULL,
                relation TEXT NOT NULL,
                quote TEXT NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                UNIQUE(source_version_id, canonical_key, content_hash)
            );

            CREATE TABLE IF NOT EXISTS extraction_candidate_observations (
                candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                run_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (candidate_id, run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_candidates_key
                ON extraction_candidates(canonical_key);
            CREATE INDEX IF NOT EXISTS idx_candidates_source
                ON extraction_candidates(source_version_id);
            """
        )
        row = self.connection.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_id'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO store_meta (key, value) VALUES ('schema_id', ?)",
                (CANDIDATE_STORE_SCHEMA,),
            )
        elif row["value"] != CANDIDATE_STORE_SCHEMA:
            raise CandidateStoreError(
                "schema_drift",
                f"candidate store holds schema {row['value']!r}, "
                f"runtime expects {CANDIDATE_STORE_SCHEMA!r}",
            )

    def persist(
        self,
        candidates: Sequence[CandidateRecord],
        *,
        run_id: str,
        observed_at: str,
    ) -> dict[str, int]:
        """Persist one batch inside a single transaction.

        Every record is verified against the deterministic key derivation
        before anything is written, so a rejected batch leaves no partial
        rows. Returns seen/persisted/deduped/observation_new counters.
        """
        if not run_id or not run_id.strip():
            raise CandidateStoreError("invalid_run_id", "run_id must be a non-empty string")
        for record in candidates:
            self._validate(record)
        seen = len(candidates)
        persisted = 0
        observations_new = 0
        with self.transaction():
            for record in candidates:
                content_hash = candidate_content_hash(
                    statement=record.statement,
                    relation=record.relation,
                    quote=record.quote,
                )
                candidate_id = candidate_id_for(
                    source_version_id=record.source_version_id,
                    canonical_key=record.canonical_key,
                    content_hash=content_hash,
                )
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO extraction_candidates (
                        candidate_id, source_version_id, doc_id, version_id,
                        canonical_key, statement, relation, quote,
                        char_start, char_end, content_hash, first_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        record.source_version_id,
                        record.doc_id,
                        record.version_id,
                        record.canonical_key,
                        record.statement,
                        record.relation,
                        record.quote,
                        record.char_start,
                        record.char_end,
                        content_hash,
                        observed_at,
                    ),
                )
                persisted += int(cursor.rowcount)
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO extraction_candidate_observations (
                        candidate_id, run_id, observed_at
                    ) VALUES (?, ?, ?)
                    """,
                    (candidate_id, run_id, observed_at),
                )
                observations_new += int(cursor.rowcount)
        return {
            "seen": seen,
            "persisted": persisted,
            "deduped": seen - persisted,
            "observations_new": observations_new,
        }

    @staticmethod
    def _validate(record: CandidateRecord) -> None:
        for field in ("statement", "quote"):
            if not getattr(record, field).strip():
                raise CandidateStoreError(
                    "invalid_candidate",
                    f"candidate {field} must be a non-empty string",
                )
        if not record.canonical_key.strip():
            raise CandidateStoreError(
                "invalid_candidate",
                "candidate canonical_key must be a non-empty string",
            )
        if record.relation not in _RELATIONS:
            raise CandidateStoreError(
                "invalid_relation",
                f"candidate relation must be supports or contradicts, got {record.relation!r}",
            )
        derived = derive_canonical_key(record.statement)
        if derived != record.canonical_key:
            raise CandidateStoreError(
                "canonical_key_mismatch",
                f"candidate canonical_key {record.canonical_key!r} does not match "
                f"derivation {derived!r} (D-031)",
            )

    def counts(self) -> dict[str, int]:
        candidates = int(
            self.connection.execute("SELECT COUNT(*) FROM extraction_candidates").fetchone()[0]
        )
        observations = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM extraction_candidate_observations"
            ).fetchone()[0]
        )
        distinct_keys = int(
            self.connection.execute(
                "SELECT COUNT(DISTINCT canonical_key) FROM extraction_candidates"
            ).fetchone()[0]
        )
        return {
            "candidates": candidates,
            "observations": observations,
            "distinct_canonical_keys": distinct_keys,
        }

    def list_candidates_for_key(self, canonical_key: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM extraction_candidates
            WHERE canonical_key = ?
            ORDER BY candidate_id
            """,
            (canonical_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_relation_conflicts(self) -> list[dict[str, Any]]:
        """Candidates sharing (source_version_id, canonical_key) that disagree on relation."""
        rows = self.connection.execute(
            """
            SELECT source_version_id, canonical_key,
                   COUNT(DISTINCT relation) AS relation_count,
                   COUNT(*) AS candidate_count
            FROM extraction_candidates
            GROUP BY source_version_id, canonical_key
            HAVING COUNT(DISTINCT relation) > 1
            ORDER BY source_version_id, canonical_key
            """
        ).fetchall()
        return [dict(row) for row in rows]
