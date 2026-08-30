from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from veritas.runtime.store import (
    RUNTIME_SCHEMA,
    RuntimeStore,
    RuntimeStoreError,
)


def _spec(item_id: str = "q1", question: str = "What retry behavior is documented?") -> dict:
    return {
        "item_id": item_id,
        "query": "retry",
        "question": question,
        "top_k": 2,
        "as_of": None,
    }


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
