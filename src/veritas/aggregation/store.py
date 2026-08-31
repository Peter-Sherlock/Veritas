"""Transactional SQLite store for claim identity clusters (M2-1, D-040).

The store maps a derived canonical key to the *representative key* of the
paraphrase cluster it belongs to. Downstream claim identity becomes
``claim_id_for(representative_key)`` instead of
``claim_id_for(canonical_key)``, so a rewording of an already-researched
fact re-enters the same claim instead of fragmenting it (C2, D-039).

Design invariants:

* The candidate store (D-032) is untouched — clustering is a downstream
  identity layer, not a rewrite of the expose-only observation record.
* A cluster's representative is frozen at creation: members attach to it,
  they never re-key it, so claim ids are stable for the life of the store.
* Clusters never merge after creation (single-pass assignment). A statement
  joins the best-scoring compatible cluster or founds a new one; the join
  decision (method, score) is persisted as the member's audit row.
* The policy is pinned at creation; reopening with a different threshold
  is a ``policy_drift`` error, because decisions made under one threshold
  are not comparable with decisions made under another.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from veritas.aggregation.clusterer import ClusterPolicy, similarity
from veritas.extraction.pipeline import derive_canonical_key


CLUSTER_STORE_SCHEMA = "claim-clusters-1"


class ClaimClusterStoreError(ValueError):
    """A stable, classifiable failure at the cluster-store boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one statement into a claim cluster."""

    canonical_key: str
    representative_key: str
    method: str  # "founder" | "lexical" | stored method of a known member
    score: float | None  # Jaccard for lexical joins, None otherwise

    @property
    def merged(self) -> bool:
        return self.representative_key != self.canonical_key


class ClaimClusterStore:
    """Transactional store mapping canonical keys to cluster representatives."""

    def __init__(self, database_path: str | Path, *, policy: ClusterPolicy | None = None) -> None:
        self.policy = policy or ClusterPolicy()
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

    def __enter__(self) -> "ClaimClusterStore":
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

            CREATE TABLE IF NOT EXISTS clusters (
                representative_key TEXT PRIMARY KEY,
                representative_statement TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cluster_members (
                cluster_id TEXT NOT NULL REFERENCES clusters(representative_key),
                canonical_key TEXT PRIMARY KEY,
                statement TEXT NOT NULL,
                method TEXT NOT NULL,
                score REAL,
                first_seen_at TEXT NOT NULL
            );
            """
        )
        stored_schema = self._meta("schema")
        if stored_schema is None:
            self.connection.executemany(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (
                    ("schema", CLUSTER_STORE_SCHEMA),
                    ("rule_version", self.policy.rule_version),
                    ("min_jaccard", repr(self.policy.min_jaccard)),
                ),
            )
            return
        if stored_schema != CLUSTER_STORE_SCHEMA:
            raise ClaimClusterStoreError(
                "schema_drift",
                f"store holds schema {stored_schema!r}, expected "
                f"{CLUSTER_STORE_SCHEMA!r}",
            )
        stored_jaccard = self._meta("min_jaccard")
        if stored_jaccard is not None and stored_jaccard != repr(self.policy.min_jaccard):
            raise ClaimClusterStoreError(
                "policy_drift",
                f"store was created with min_jaccard={stored_jaccard}, "
                f"requested {self.policy.min_jaccard!r}; decisions under "
                "different thresholds are not comparable",
            )

    def _meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM store_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def resolve(
        self, *, canonical_key: str, statement: str, observed_at: str
    ) -> Resolution:
        """Map a canonical key to its cluster representative, creating or
        joining a cluster when the key is new. Idempotent per key."""
        if not statement.strip():
            raise ClaimClusterStoreError(
                "invalid_statement", "statement must be a non-empty string"
            )
        derived = derive_canonical_key(statement)
        if derived != canonical_key:
            raise ClaimClusterStoreError(
                "canonical_key_mismatch",
                f"canonical_key {canonical_key!r} does not match the "
                f"statement-derived key {derived!r}",
            )
        with self.transaction():
            known = self.connection.execute(
                """
                SELECT cluster_id, method, score FROM cluster_members
                WHERE canonical_key = ?
                """,
                (canonical_key,),
            ).fetchone()
            if known is not None:
                return Resolution(
                    canonical_key=canonical_key,
                    representative_key=str(known["cluster_id"]),
                    method=str(known["method"]),
                    score=None if known["score"] is None else float(known["score"]),
                )

            best: tuple[float, str] | None = None
            for cluster in self.connection.execute(
                """
                SELECT representative_key, representative_statement
                FROM clusters ORDER BY representative_key
                """
            ):
                score = similarity(statement, str(cluster["representative_statement"]))
                if score is None or score < self.policy.min_jaccard:
                    continue
                candidate = (score, str(cluster["representative_key"]))
                if best is None or candidate[0] > best[0] or (
                    candidate[0] == best[0] and candidate[1] < best[1]
                ):
                    best = candidate
            if best is not None:
                score, representative_key = best
                self.connection.execute(
                    """
                    INSERT INTO cluster_members (
                        cluster_id, canonical_key, statement, method, score, first_seen_at
                    ) VALUES (?, ?, ?, 'lexical', ?, ?)
                    """,
                    (representative_key, canonical_key, statement, score, observed_at),
                )
                return Resolution(
                    canonical_key=canonical_key,
                    representative_key=representative_key,
                    method="lexical",
                    score=score,
                )

            self.connection.execute(
                """
                INSERT INTO clusters (
                    representative_key, representative_statement, created_at
                ) VALUES (?, ?, ?)
                """,
                (canonical_key, statement, observed_at),
            )
            self.connection.execute(
                """
                INSERT INTO cluster_members (
                    cluster_id, canonical_key, statement, method, score, first_seen_at
                ) VALUES (?, ?, ?, 'founder', NULL, ?)
                """,
                (canonical_key, canonical_key, statement, observed_at),
            )
            return Resolution(
                canonical_key=canonical_key,
                representative_key=canonical_key,
                method="founder",
                score=None,
            )

    def representative_key(self, canonical_key: str) -> str | None:
        """Look up a known mapping without creating anything."""
        row = self.connection.execute(
            "SELECT cluster_id FROM cluster_members WHERE canonical_key = ?",
            (canonical_key,),
        ).fetchone()
        return None if row is None else str(row["cluster_id"])

    def find_cluster(self, canonical_key: str) -> tuple[str, ...]:
        """All member keys of the cluster containing ``canonical_key``."""
        cluster_id = self.representative_key(canonical_key)
        if cluster_id is None:
            return ()
        rows = self.connection.execute(
            "SELECT canonical_key FROM cluster_members WHERE cluster_id = ? "
            "ORDER BY canonical_key",
            (cluster_id,),
        ).fetchall()
        return tuple(str(row["canonical_key"]) for row in rows)

    def cluster_statement(self, cluster_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT representative_statement FROM clusters WHERE representative_key = ?",
            (cluster_id,),
        ).fetchone()
        return None if row is None else str(row["representative_statement"])

    def cluster_created_at(self, cluster_id: str) -> str | None:
        """Return the founder timestamp frozen with the representative identity."""
        row = self.connection.execute(
            "SELECT created_at FROM clusters WHERE representative_key = ?",
            (cluster_id,),
        ).fetchone()
        return None if row is None else str(row["created_at"])

    def counts(self) -> dict[str, int]:
        return {
            "clusters": int(
                self.connection.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
            ),
            "members": int(
                self.connection.execute("SELECT COUNT(*) FROM cluster_members").fetchone()[0]
            ),
        }
