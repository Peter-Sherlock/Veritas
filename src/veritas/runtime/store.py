"""Session store for the research runtime.

The runtime owns session state (queue, checkpoint, budget) in its own
SQLite file, separate from the candidate store (D-032) and the P0 evolution
repository: checkpoints are mutable by nature while evidence stays
append-only, and losing the separation would let either concern corrupt the
other's invariants.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from veritas.extraction.models import ExtractionCandidateBundle

RUNTIME_SCHEMA = "research-runtime-3"
_PREVIOUS_RUNTIME_SCHEMA = "research-runtime-2"
SESSION_ACTIVE = "active"
SESSION_COMPLETED = "completed"
SESSION_BUDGET_EXHAUSTED = "budget_exhausted"
_ITEM_PENDING = "pending"
_ITEM_COMPLETED = "completed"
_ITEM_REJECTED = "rejected"
_OUTPUT_PENDING = "pending"
_OUTPUT_APPLIED = "applied"
_OUTPUT_IGNORED = "ignored"


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
                effective_top_k INTEGER NOT NULL,
                PRIMARY KEY (session_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS item_outputs (
                session_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                bundle_hash TEXT NOT NULL,
                bundle_json TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                persisted_at TEXT NOT NULL,
                delivered_at TEXT,
                refresh_id TEXT,
                PRIMARY KEY (session_id, item_id),
                FOREIGN KEY (session_id, item_id)
                    REFERENCES work_items(session_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS session_contexts (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                namespace TEXT NOT NULL,
                context_hash TEXT NOT NULL,
                context_json TEXT NOT NULL,
                bound_at TEXT NOT NULL,
                PRIMARY KEY (session_id, namespace)
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
        elif row["value"] == _PREVIOUS_RUNTIME_SCHEMA:
            # research-runtime-3 is an additive migration: v2 already has
            # effective_top_k, and the DDL above adds only the durable outbox.
            self.connection.execute(
                "UPDATE runtime_meta SET value = ? WHERE key = 'schema_id'",
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
                            as_of, status, attempts, last_error, completed_at,
                            effective_top_k
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)
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
                            item["top_k"],
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

    def find_session(self, session_id: str) -> dict[str, Any] | None:
        """Session state or None when the session does not exist yet."""
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        state = dict(row)
        state["items"] = self.list_items(session_id)
        return state

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

    def get_item(self, session_id: str, item_id: str) -> dict[str, Any]:
        self._require_item(session_id, item_id)
        row = self.connection.execute(
            "SELECT * FROM work_items WHERE session_id = ? AND item_id = ?",
            (session_id, item_id),
        ).fetchone()
        return dict(row)

    def list_items(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM work_items WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def session_spec(self, session_id: str) -> dict[str, Any]:
        """The immutable work specification stored for a resumable session."""
        state = self.session_state(session_id)
        return {
            "session_id": session_id,
            "budget_requests": int(state["budget_requests"]),
            "items": [
                {
                    "item_id": item["item_id"],
                    "query": item["query"],
                    "question": item["question"],
                    "top_k": int(item["top_k"]),
                    "as_of": item["as_of"],
                }
                for item in state["items"]
            ],
        }

    def get_session_context(
        self, session_id: str, namespace: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT context_json FROM session_contexts "
            "WHERE session_id = ? AND namespace = ?",
            (session_id, namespace),
        ).fetchone()
        return None if row is None else dict(json.loads(row["context_json"]))

    def bind_session_context(
        self,
        session_id: str,
        namespace: str,
        context: dict[str, Any],
        *,
        bound_at: str,
    ) -> dict[str, Any]:
        """Bind an exact orchestration context to a runtime session once."""
        context_json = json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        with self.transaction():
            if self.find_session(session_id) is None:
                raise RuntimeStoreError(
                    "unknown_session", f"unknown session: {session_id}"
                )
            row = self.connection.execute(
                "SELECT context_hash, context_json FROM session_contexts "
                "WHERE session_id = ? AND namespace = ?",
                (session_id, namespace),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO session_contexts "
                    "(session_id, namespace, context_hash, context_json, bound_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, namespace, context_hash, context_json, bound_at),
                )
            elif (
                row["context_hash"] != context_hash
                or row["context_json"] != context_json
            ):
                raise RuntimeStoreError(
                    "session_context_drift",
                    f"session {session_id!r} context {namespace!r} is already bound",
                )
        return dict(context)

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

    def complete_item_with_output(
        self,
        session_id: str,
        item_id: str,
        bundle: ExtractionCandidateBundle,
        observed_at: str,
    ) -> dict[str, Any]:
        """Atomically persist a validated bundle and make its item terminal.

        The output row is the runtime-to-evolution outbox. A completed item
        therefore never exists without the exact bundle needed to resume graph
        delivery after a process interruption.
        """
        bundle_json = json.dumps(
            bundle.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        bundle_hash = hashlib.sha256(bundle_json.encode("utf-8")).hexdigest()
        with self.transaction():
            self._require_item(session_id, item_id)
            item = self.connection.execute(
                "SELECT query, question, status FROM work_items "
                "WHERE session_id = ? AND item_id = ?",
                (session_id, item_id),
            ).fetchone()
            if item["query"] != bundle.query or item["question"] != bundle.question:
                raise RuntimeStoreError(
                    "output_spec_mismatch",
                    f"bundle for {session_id!r}/{item_id!r} does not match its work item",
                )
            if item["status"] not in (_ITEM_PENDING, _ITEM_COMPLETED):
                raise RuntimeStoreError(
                    "invalid_item_transition",
                    f"cannot attach output to {item['status']!r} item {item_id!r}",
                )
            prior = self.connection.execute(
                "SELECT bundle_hash, bundle_json FROM item_outputs "
                "WHERE session_id = ? AND item_id = ?",
                (session_id, item_id),
            ).fetchone()
            if prior is None:
                self.connection.execute(
                    """
                    INSERT INTO item_outputs (
                        session_id, item_id, bundle_hash, bundle_json,
                        delivery_status, persisted_at, delivered_at, refresh_id
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        session_id,
                        item_id,
                        bundle_hash,
                        bundle_json,
                        _OUTPUT_PENDING,
                        observed_at,
                    ),
                )
            elif (
                prior["bundle_hash"] != bundle_hash
                or prior["bundle_json"] != bundle_json
            ):
                raise RuntimeStoreError(
                    "output_conflict",
                    f"item {session_id!r}/{item_id!r} already has a different bundle",
                )
            self.connection.execute(
                """
                UPDATE work_items
                SET status = ?, completed_at = ?, last_error = NULL
                WHERE session_id = ? AND item_id = ?
                """,
                (_ITEM_COMPLETED, observed_at, session_id, item_id),
            )
        return self.get_item_output(session_id, item_id)

    def get_item_output(self, session_id: str, item_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM item_outputs WHERE session_id = ? AND item_id = ?",
            (session_id, item_id),
        ).fetchone()
        if row is None:
            raise RuntimeStoreError(
                "unknown_output",
                f"session {session_id!r} item {item_id!r} has no persisted output",
            )
        result = dict(row)
        result["bundle"] = ExtractionCandidateBundle.from_dict(
            json.loads(result.pop("bundle_json"))
        )
        return result

    def list_item_outputs(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT output.*
            FROM item_outputs AS output
            JOIN work_items AS item
              ON item.session_id = output.session_id
             AND item.item_id = output.item_id
            WHERE output.session_id = ?
            ORDER BY item.seq
            """,
            (session_id,),
        ).fetchall()
        outputs: list[dict[str, Any]] = []
        for row in rows:
            output = dict(row)
            output["bundle"] = ExtractionCandidateBundle.from_dict(
                json.loads(output.pop("bundle_json"))
            )
            outputs.append(output)
        return outputs

    def mark_item_output_delivered(
        self,
        session_id: str,
        item_id: str,
        *,
        delivery_status: str,
        delivered_at: str,
        refresh_id: str | None,
    ) -> None:
        if delivery_status not in (_OUTPUT_APPLIED, _OUTPUT_IGNORED):
            raise RuntimeStoreError(
                "invalid_output_status",
                f"unsupported output delivery status {delivery_status!r}",
            )
        if delivery_status == _OUTPUT_APPLIED and not refresh_id:
            raise RuntimeStoreError(
                "invalid_output_status", "an applied output needs a refresh_id"
            )
        with self.transaction():
            row = self.connection.execute(
                "SELECT delivery_status, refresh_id FROM item_outputs "
                "WHERE session_id = ? AND item_id = ?",
                (session_id, item_id),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError(
                    "unknown_output",
                    f"session {session_id!r} item {item_id!r} has no persisted output",
                )
            if row["delivery_status"] != _OUTPUT_PENDING:
                if (
                    row["delivery_status"] == delivery_status
                    and row["refresh_id"] == refresh_id
                ):
                    return
                raise RuntimeStoreError(
                    "output_delivery_conflict",
                    f"item {session_id!r}/{item_id!r} was already delivered "
                    f"as {row['delivery_status']!r}",
                )
            self.connection.execute(
                """
                UPDATE item_outputs
                SET delivery_status = ?, delivered_at = ?, refresh_id = ?
                WHERE session_id = ? AND item_id = ?
                """,
                (
                    delivery_status,
                    delivered_at,
                    refresh_id,
                    session_id,
                    item_id,
                ),
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

    def requeue_item(self, session_id: str, item_id: str, new_top_k: int) -> None:
        """Put a rejected item back into the queue with a degraded top_k.

        The replanned top_k is persisted immediately, so an interrupted
        retry resumes at the degraded breadth instead of re-spending at the
        original one. The previous rejection code is cleared: the item is
        pending again, and its retry history stays visible via ``attempts``.
        """
        if not isinstance(new_top_k, int) or new_top_k < 1:
            raise RuntimeStoreError("invalid_item", "requeue needs a top_k >= 1")
        with self.transaction():
            self._require_item(session_id, item_id)
            self.connection.execute(
                """
                UPDATE work_items
                SET status = ?, effective_top_k = ?, last_error = NULL,
                    completed_at = NULL
                WHERE session_id = ? AND item_id = ?
                """,
                (_ITEM_PENDING, new_top_k, session_id, item_id),
            )

    def degrade_queue_to_fit(
        self,
        session_id: str,
        available_requests: int,
        *,
        min_top_k: int,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        """Degrade pending items' effective top_k until the worst case fits.

        Deterministic: while the queue's worst-case request count exceeds
        the available budget, the pending item with the largest effective
        top_k (ties broken by queue order) is reduced by one, floored at
        ``min_top_k``. Items that cannot fit even at the floor stay as they
        are — the run will then stop at the budget as usual. Only pending
        items are touched and every reduction is persisted in the same
        transaction, so resume sees exactly the replanned queue.
        """
        if available_requests < 0:
            raise RuntimeStoreError("invalid_budget", "available_requests must be >= 0")
        degraded: list[dict[str, Any]] = []
        with self.transaction():
            rows = [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT item_id, seq, effective_top_k FROM work_items
                    WHERE session_id = ? AND status = ?
                    ORDER BY seq
                    """,
                    (session_id, _ITEM_PENDING),
                )
            ]
            worst_case = sum(int(row["effective_top_k"]) for row in rows)
            while worst_case > available_requests:
                candidates = [row for row in rows if int(row["effective_top_k"]) > min_top_k]
                if not candidates:
                    break
                target = max(candidates, key=lambda row: (int(row["effective_top_k"]), -row["seq"]))
                target["effective_top_k"] = int(target["effective_top_k"]) - 1
                worst_case -= 1
                self.connection.execute(
                    "UPDATE work_items SET effective_top_k = ? "
                    "WHERE session_id = ? AND item_id = ?",
                    (target["effective_top_k"], session_id, target["item_id"]),
                )
                if target not in degraded:
                    degraded.append(target)
        return degraded

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
