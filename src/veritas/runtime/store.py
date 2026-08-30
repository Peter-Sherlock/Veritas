"""Session store for the research runtime.

The runtime owns session state (queue, checkpoint, budget) in its own
SQLite file, separate from the candidate store (D-032) and the P0 evolution
repository: checkpoints are mutable by nature while evidence stays
append-only, and losing the separation would let either concern corrupt the
other's invariants.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


RUNTIME_SCHEMA = "research-runtime-1"
SESSION_ACTIVE = "active"
SESSION_COMPLETED = "completed"
SESSION_BUDGET_EXHAUSTED = "budget_exhausted"
_ITEM_PENDING = "pending"
_ITEM_COMPLETED = "completed"
_ITEM_REJECTED = "rejected"


class RuntimeStoreError(ValueError):
    """A stable, classifiable failure at the runtime-store boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class RuntimeStore:
    """Transactional SQLite store for research session state.

    A session is a work queue of research items plus a request budget.
    Every item transition is its own checkpoint transaction, so an
    interrupted run resumes without redoing terminal items. Budget
    accounting is reserve-then-call: ``try_reserve_request`` atomically
    increments the persisted counter only while it is below the budget, so
    a crash can never cause overspending — a request that was reserved but
    not answered still counts as spent.
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

    def __enter__(self) -> "RuntimeStore":
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
            CREATE TABLE IF NOT EXISTS runtime_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                budget_requests INTEGER NOT NULL,
                requests_spent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS work_items (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                item_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                query TEXT NOT NULL,
                question TEXT NOT NULL,
                top_k INTEGER NOT NULL,
                as_of TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                completed_at TEXT,
                PRIMARY KEY (session_id, item_id)
            );
            """
        )
        row = self.connection.execute(
            "SELECT value FROM runtime_meta WHERE key = 'schema_id'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO runtime_meta (key, value) VALUES ('schema_id', ?)",
                (RUNTIME_SCHEMA,),
            )
        elif row["value"] != RUNTIME_SCHEMA:
            raise RuntimeStoreError(
                "schema_drift",
                f"runtime store holds schema {row['value']!r}, "
                f"runtime expects {RUNTIME_SCHEMA!r}",
            )

    def create_session(
        self,
        *,
        session_id: str,
        items: Sequence[dict[str, Any]],
        budget_requests: int,
        observed_at: str,
    ) -> dict[str, Any]:
        """Create the session or validate a resume against the stored spec.

        Re-opening an existing session requires the exact same item spec
        (identity, query, question, top_k, as_of) and a budget that does not
        decrease; raising the budget of an exhausted session reactivates it.
        Returns the current session state.
        """
        if not session_id or not session_id.strip():
            raise RuntimeStoreError("invalid_session", "session_id must be a non-empty string")
        if not isinstance(budget_requests, int) or budget_requests < 1:
            raise RuntimeStoreError(
                "invalid_budget", "budget_requests must be a positive integer"
            )
        if not items:
            raise RuntimeStoreError("invalid_session", "a session needs at least one work item")
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for seq, item in enumerate(items):
            item_id = item["item_id"]
            if not item_id or not item_id.strip():
                raise RuntimeStoreError("invalid_item", "item_id must be a non-empty string")
            if item_id in seen_ids:
                raise RuntimeStoreError("invalid_item", f"duplicate item_id {item_id!r}")
            seen_ids.add(item_id)
            if not item["query"].strip() or not item["question"].strip():
                raise RuntimeStoreError(
                    "invalid_item", f"item {item_id!r} needs a query and a question"
                )
            if not isinstance(item["top_k"], int) or item["top_k"] < 1:
                raise RuntimeStoreError("invalid_item", f"item {item_id!r} needs top_k >= 1")
            normalized.append({**item, "seq": seq})

        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, status, budget_requests, requests_spent,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    (session_id, SESSION_ACTIVE, budget_requests, observed_at, observed_at),
                )
                for item in normalized:
                    self.connection.execute(
                        """
                        INSERT INTO work_items (
                            session_id, item_id, seq, query, question, top_k,
                            as_of, status, attempts, last_error, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
                        """,
                        (
                            session_id,
                            item["item_id"],
                            item["seq"],
                            item["query"],
                            item["question"],
                            item["top_k"],
                            item.get("as_of"),
                            _ITEM_PENDING,
                        ),
                    )
            else:
                self._validate_resume(session_id, normalized)
                if budget_requests < int(row["budget_requests"]):
                    raise RuntimeStoreError(
                        "budget_decrease",
                        f"budget_requests must not decrease on resume "
                        f"(stored {row['budget_requests']}, got {budget_requests})",
                    )
                if budget_requests > int(row["budget_requests"]):
                    self.connection.execute(
                        "UPDATE sessions SET budget_requests = ? WHERE session_id = ?",
                        (budget_requests, session_id),
                    )
                    if row["status"] == SESSION_BUDGET_EXHAUSTED:
                        self.connection.execute(
                            "UPDATE sessions SET status = ? WHERE session_id = ?",
                            (SESSION_ACTIVE, session_id),
                        )
        return self.session_state(session_id)

    def _validate_resume(
        self,
        session_id: str,
        normalized: Sequence[dict[str, Any]],
    ) -> None:
        stored = self.connection.execute(
            """
            SELECT item_id, query, question, top_k, as_of
            FROM work_items WHERE session_id = ? ORDER BY seq
            """,
            (session_id,),
        ).fetchall()
        stored_spec = {
            (
                row["item_id"],
                row["query"],
                row["question"],
                int(row["top_k"]),
                row["as_of"],
            )
            for row in stored
        }
        incoming_spec = {
            (
                item["item_id"],
                item["query"],
                item["question"],
                int(item["top_k"]),
                item.get("as_of"),
            )
            for item in normalized
        }
        if stored_spec != incoming_spec or len(stored) != len(normalized):
            raise RuntimeStoreError(
                "session_spec_drift",
                f"resume spec for session {session_id!r} does not match the stored queue",
            )

    def try_reserve_request(self, session_id: str) -> bool:
        """Atomically reserve one LLM request against the session budget."""
        cursor = self.connection.execute(
            """
            UPDATE sessions
            SET requests_spent = requests_spent + 1
            WHERE session_id = ? AND requests_spent < budget_requests
            """,
            (session_id,),
        )
        return cursor.rowcount == 1

    def session_state(self, session_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise RuntimeStoreError("unknown_session", f"unknown session: {session_id}")
        state = dict(row)
        state["items"] = self.list_items(session_id)
        return state

    def list_items(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM work_items WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def pending_items(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM work_items
            WHERE session_id = ? AND status = ?
            ORDER BY seq
            """,
            (session_id, _ITEM_PENDING),
        ).fetchall()
        return [dict(row) for row in rows]

    def _require_item(self, session_id: str, item_id: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM work_items WHERE session_id = ? AND item_id = ?",
            (session_id, item_id),
        ).fetchone()
        if row is None:
            raise RuntimeStoreError(
                "unknown_item",
                f"session {session_id!r} has no work item {item_id!r}",
            )

    def start_item(self, session_id: str, item_id: str) -> None:
        """Count one processing attempt; the item stays pending until it lands."""
        with self.transaction():
            self._require_item(session_id, item_id)
            self.connection.execute(
                "UPDATE work_items SET attempts = attempts + 1 "
                "WHERE session_id = ? AND item_id = ?",
                (session_id, item_id),
            )

    def mark_item_completed(self, session_id: str, item_id: str, observed_at: str) -> None:
        with self.transaction():
            self._require_item(session_id, item_id)
            self.connection.execute(
                """
                UPDATE work_items
                SET status = ?, completed_at = ?, last_error = NULL
                WHERE session_id = ? AND item_id = ?
                """,
                (_ITEM_COMPLETED, observed_at, session_id, item_id),
            )

    def mark_item_rejected(
        self, session_id: str, item_id: str, code: str, observed_at: str
    ) -> None:
        with self.transaction():
            self._require_item(session_id, item_id)
            self.connection.execute(
                """
                UPDATE work_items
                SET status = ?, completed_at = ?, last_error = ?
                WHERE session_id = ? AND item_id = ?
                """,
                (_ITEM_REJECTED, observed_at, code, session_id, item_id),
            )

    def mark_session_budget_exhausted(self, session_id: str, observed_at: str) -> None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT status FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError("unknown_session", f"unknown session: {session_id}")
            if row["status"] == SESSION_ACTIVE:
                self.connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                    (SESSION_BUDGET_EXHAUSTED, observed_at, session_id),
                )

    def mark_session_completed(self, session_id: str, observed_at: str) -> None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT status FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError("unknown_session", f"unknown session: {session_id}")
            pending = self.connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE session_id = ? AND status = ?",
                (session_id, _ITEM_PENDING),
            ).fetchone()[0]
            if pending:
                raise RuntimeStoreError(
                    "pending_items_remain",
                    f"session {session_id!r} still has {pending} pending items",
                )
            if row["status"] == SESSION_ACTIVE:
                self.connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                    (SESSION_COMPLETED, observed_at, session_id),
                )

    def counts(self) -> dict[str, int]:
        sessions = int(
            self.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        )
        by_status: dict[str, int] = {}
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS n FROM work_items GROUP BY status"
        ):
            by_status[row["status"]] = int(row["n"])
        return {
            "sessions": sessions,
            "work_items": sum(by_status.values()),
            "items_pending": by_status.get(_ITEM_PENDING, 0),
            "items_completed": by_status.get(_ITEM_COMPLETED, 0),
            "items_rejected": by_status.get(_ITEM_REJECTED, 0),
        }
