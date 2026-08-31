from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from veritas.extraction.models import ExtractionCandidateBundle
from veritas.runtime.store import (
    RUNTIME_SCHEMA,
    RuntimeStore,
    RuntimeStoreError,
)
from veritas.search.provider import SearchResult


def _spec(item_id: str = "q1", question: str = "What retry behavior is documented?") -> dict:
    return {
        "item_id": item_id,
        "query": "retry",
        "question": question,
        "top_k": 2,
        "as_of": None,
    }


def _bundle(*, with_result: bool = False) -> ExtractionCandidateBundle:
    return ExtractionCandidateBundle(
        query="retry",
        question="What retry behavior is documented?",
        retrieved=(
            (
                SearchResult(
                    doc_id="retry",
                    version_id="1.0",
                    title="Retry",
                    path=Path("retry/1.0.md"),
                    score=1.0,
                    snippet="retry",
                ),
            )
            if with_result
            else ()
        ),
        documents=(),
        evidence_spans=(),
        claims=(),
        edges=(),
    )


class RuntimeStoreTests(unittest.TestCase):
    def test_store_declares_its_schema_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.db"
            with RuntimeStore(path) as store:
                row = store.connection.execute(
                    "SELECT value FROM runtime_meta WHERE key = 'schema_id'"
                ).fetchone()
                self.assertEqual(RUNTIME_SCHEMA, row["value"])
                store.connection.execute(
                    "UPDATE runtime_meta SET value = 'research-runtime-0' "
                    "WHERE key = 'schema_id'"
                )
            with self.assertRaises(RuntimeStoreError) as caught:
                RuntimeStore(path)
            self.assertEqual("schema_drift", caught.exception.code)

    def test_v2_store_migrates_additively_to_the_outbox_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.db"
            with RuntimeStore(path) as store:
                store.connection.execute(
                    "UPDATE runtime_meta SET value = 'research-runtime-2' "
                    "WHERE key = 'schema_id'"
                )
                store.connection.execute("DROP TABLE item_outputs")
            with RuntimeStore(path) as migrated:
                row = migrated.connection.execute(
                    "SELECT value FROM runtime_meta WHERE key = 'schema_id'"
                ).fetchone()
                self.assertEqual(RUNTIME_SCHEMA, row["value"])
                table = migrated.connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'item_outputs'"
                ).fetchone()
                self.assertIsNotNone(table)
                context_table = migrated.connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'session_contexts'"
                ).fetchone()
                self.assertIsNotNone(context_table)

    def test_session_context_is_bound_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=5,
                    observed_at="2026-08-30T00:00:00Z",
                )
                context = {"rule_version": "rules-1", "observed_at": "t0"}
                store.bind_session_context(
                    "s1", "watch", context, bound_at="2026-08-30T00:00:00Z"
                )
                self.assertEqual(context, store.get_session_context("s1", "watch"))
                store.bind_session_context(
                    "s1", "watch", context, bound_at="2026-08-30T00:01:00Z"
                )
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.bind_session_context(
                        "s1",
                        "watch",
                        {**context, "rule_version": "rules-2"},
                        bound_at="2026-08-30T00:02:00Z",
                    )
                self.assertEqual("session_context_drift", caught.exception.code)

    def test_create_session_validates_the_spec(self) -> None:
        cases = [
            ([], "invalid_session"),
            ([_spec("q1"), _spec("q1")], "invalid_item"),
            ([{**_spec("q1"), "top_k": 0}], "invalid_item"),
            ([{**_spec("q1"), "question": "  "}], "invalid_item"),
        ]
        for items, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as tmp:
                    with RuntimeStore(Path(tmp) / "runtime.db") as store:
                        with self.assertRaises(RuntimeStoreError) as caught:
                            store.create_session(
                                session_id="s1",
                                items=items,
                                budget_requests=5,
                                observed_at="2026-08-30T00:00:00Z",
                            )
                        self.assertEqual(expected_code, caught.exception.code)
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.create_session(
                        session_id="s1",
                        items=[_spec()],
                        budget_requests=0,
                        observed_at="2026-08-30T00:00:00Z",
                    )
                self.assertEqual("invalid_budget", caught.exception.code)

    def test_resume_requires_the_exact_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=5,
                    observed_at="2026-08-30T00:00:00Z",
                )
                state = store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=5,
                    observed_at="2026-08-30T01:00:00Z",
                )
                self.assertEqual("active", state["status"])
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.create_session(
                        session_id="s1",
                        items=[_spec(question="Different question?")],
                        budget_requests=5,
                        observed_at="2026-08-30T01:00:00Z",
                    )
                self.assertEqual("session_spec_drift", caught.exception.code)

    def test_request_reservation_is_atomic_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.db"
            with RuntimeStore(path) as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=3,
                    observed_at="2026-08-30T00:00:00Z",
                )
                self.assertTrue(store.try_reserve_request("s1"))
                self.assertTrue(store.try_reserve_request("s1"))
                self.assertTrue(store.try_reserve_request("s1"))
                self.assertFalse(store.try_reserve_request("s1"))
                self.assertFalse(store.try_reserve_request("s1"))
            with RuntimeStore(path) as reopened:
                self.assertEqual(3, reopened.session_state("s1")["requests_spent"])
                # An unknown session has no budget to reserve against.
                self.assertFalse(reopened.try_reserve_request("missing"))

    def test_item_transitions_are_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec("q1"), _spec("q2")],
                    budget_requests=5,
                    observed_at="2026-08-30T00:00:00Z",
                )
                store.start_item("s1", "q1")
                store.mark_item_completed("s1", "q1", "2026-08-30T00:01:00Z")
                store.start_item("s1", "q2")
                store.mark_item_rejected("s1", "q2", "invalid_json", "2026-08-30T00:02:00Z")
                items = {item["item_id"]: item for item in store.list_items("s1")}
                self.assertEqual(1, items["q1"]["attempts"])
                self.assertEqual("completed", items["q1"]["status"])
                self.assertIsNone(items["q1"]["last_error"])
                self.assertEqual(1, items["q2"]["attempts"])
                self.assertEqual("rejected", items["q2"]["status"])
                self.assertEqual("invalid_json", items["q2"]["last_error"])
                self.assertEqual(
                    {
                        "sessions": 1,
                        "work_items": 2,
                        "items_pending": 0,
                        "items_completed": 1,
                        "items_rejected": 1,
                    },
                    store.counts(),
                )
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.mark_item_completed("s1", "missing", "2026-08-30T00:03:00Z")
                self.assertEqual("unknown_item", caught.exception.code)

    def test_output_and_terminal_item_are_one_rehydratable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.db"
            with RuntimeStore(path) as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=5,
                    observed_at="2026-08-30T00:00:00Z",
                )
                store.start_item("s1", "q1")
                output = store.complete_item_with_output(
                    "s1", "q1", _bundle(), "2026-08-30T00:01:00Z"
                )
                self.assertEqual("completed", store.get_item("s1", "q1")["status"])
                self.assertEqual("pending", output["delivery_status"])
                self.assertEqual(64, len(output["bundle_hash"]))
                self.assertEqual(_bundle().to_dict(), output["bundle"].to_dict())
            with RuntimeStore(path) as reopened:
                output = reopened.get_item_output("s1", "q1")
                self.assertEqual(_bundle().to_dict(), output["bundle"].to_dict())
                reopened.mark_item_output_delivered(
                    "s1",
                    "q1",
                    delivery_status="ignored",
                    delivered_at="2026-08-30T00:02:00Z",
                    refresh_id=None,
                )
                # Delivery acknowledgement is itself idempotent.
                reopened.mark_item_output_delivered(
                    "s1",
                    "q1",
                    delivery_status="ignored",
                    delivered_at="2026-08-30T00:03:00Z",
                    refresh_id=None,
                )
                self.assertEqual(
                    "ignored",
                    reopened.get_item_output("s1", "q1")["delivery_status"],
                )

    def test_item_output_conflict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=5,
                    observed_at="2026-08-30T00:00:00Z",
                )
                store.complete_item_with_output(
                    "s1", "q1", _bundle(), "2026-08-30T00:01:00Z"
                )
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.complete_item_with_output(
                        "s1",
                        "q1",
                        _bundle(with_result=True),
                        "2026-08-30T00:02:00Z",
                    )
                self.assertEqual("output_conflict", caught.exception.code)

    def test_output_spec_mismatch_rolls_back_the_terminal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=5,
                    observed_at="2026-08-30T00:00:00Z",
                )
                store.start_item("s1", "q1")
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.complete_item_with_output(
                        "s1",
                        "q1",
                        replace(_bundle(), query="a different query"),
                        "2026-08-30T00:01:00Z",
                    )
                self.assertEqual("output_spec_mismatch", caught.exception.code)
                self.assertEqual("pending", store.get_item("s1", "q1")["status"])
                self.assertEqual([], store.list_item_outputs("s1"))

    def test_budget_is_monotonic_and_exhaustion_can_be_lifted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=3,
                    observed_at="2026-08-30T00:00:00Z",
                )
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.create_session(
                        session_id="s1",
                        items=[_spec()],
                        budget_requests=2,
                        observed_at="2026-08-30T00:01:00Z",
                    )
                self.assertEqual("budget_decrease", caught.exception.code)
                store.mark_session_budget_exhausted("s1", "2026-08-30T00:02:00Z")
                self.assertEqual(
                    "budget_exhausted", store.session_state("s1")["status"]
                )
                # Same budget keeps the exhausted state; raising it reactivates.
                state = store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=3,
                    observed_at="2026-08-30T00:03:00Z",
                )
                self.assertEqual("budget_exhausted", state["status"])
                state = store.create_session(
                    session_id="s1",
                    items=[_spec()],
                    budget_requests=4,
                    observed_at="2026-08-30T00:04:00Z",
                )
                self.assertEqual("active", state["status"])
                self.assertEqual(4, state["budget_requests"])

    def test_completed_session_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                store.create_session(
                    session_id="s1",
                    items=[_spec("q1"), _spec("q2")],
                    budget_requests=5,
                    observed_at="2026-08-30T00:00:00Z",
                )
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.mark_session_completed("s1", "2026-08-30T00:01:00Z")
                self.assertEqual("pending_items_remain", caught.exception.code)
                store.mark_item_completed("s1", "q1", "2026-08-30T00:01:00Z")
                store.mark_item_completed("s1", "q2", "2026-08-30T00:02:00Z")
                store.mark_session_completed("s1", "2026-08-30T00:03:00Z")
                self.assertEqual("completed", store.session_state("s1")["status"])
                with self.assertRaises(RuntimeStoreError) as caught:
                    store.session_state("missing")
                self.assertEqual("unknown_session", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
