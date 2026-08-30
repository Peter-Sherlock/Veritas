from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.extraction.store import CandidateStore
from veritas.providers.llm import LLMResponse
from veritas.runtime import (
    ReplanPolicy,
    ResearchRuntime,
    RuntimeStore,
    WorkItem,
)
from veritas.search.provider import SearchResult, VersionedDocument


REASONED_AT = "2026-08-30T00:00:00Z"


def _document(doc_id: str, content: str) -> VersionedDocument:
    return VersionedDocument(
        doc_id=doc_id,
        version_id="1.0",
        title=doc_id,
        content=content,
        published_at="2026-08-01T00:00:00Z",
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


class _MultiDocSearch:
    """Serves a fixed number of documents per query, most relevant first."""

    def __init__(self, total: int = 3) -> None:
        self.total = total
        self._corpus: dict[str, tuple[VersionedDocument, ...]] = {}

    def _docs_for(self, query: str) -> tuple[VersionedDocument, ...]:
        if query not in self._corpus:
            self._corpus[query] = tuple(
                _document(
                    f"{query}-{i}",
                    f"{query} document {i} states HTTPX fact number {i}.",
                )
                for i in range(self.total)
            )
        return self._corpus[query]

    def search(self, query: str, *, top_k: int = 5, as_of: str | None = None):
        return [
            SearchResult(
                doc_id=doc.doc_id,
                version_id=doc.version_id,
                title=doc.title,
                path=Path("fixture.md"),
                score=1.0,
                snippet=doc.content,
            )
            for doc in self._docs_for(query)
        ][:top_k]

    def fetch(self, doc_id: str, version_id: str) -> VersionedDocument:
        query, index = doc_id.rsplit("-", 1)
        doc = self._docs_for(query)[int(index)]
        if version_id != doc.version_id:
            raise KeyError((doc_id, version_id))
        return doc


class _PerDocLLM:
    """Valid assertion per document; broken doc ids get an unfindable quote."""

    def __init__(self, broken_ids=()) -> None:
        self.model_id = "replan-model"
        self.broken_ids = set(broken_ids)
        self.calls: list[str] = []

    def complete(self, *, system: str, prompt: str, json_mode: bool = True):
        self.calls.append(prompt)
        document = json.loads(prompt)["document"]
        doc_id = document["doc_id"]
        if doc_id in self.broken_ids:
            payload = {
                "assertions": [
                    {
                        "statement": "Anything at all",
                        "relation": "supports",
                        "quote": "this quote is not a substring of the document",
                    }
                ]
            }
        else:
            index = doc_id.rsplit("-", 1)[1]
            payload = {
                "assertions": [
                    {
                        "statement": f"HTTPX fact number {index} applies",
                        "relation": "supports",
                        "quote": document["content"],
                    }
                ]
            }
        return LLMResponse(
            text=json.dumps(payload),
            model_id=self.model_id,
            prompt_tokens=1,
            completion_tokens=1,
        )


def _items() -> list[WorkItem]:
    return [
        WorkItem(item_id="item-a", query="alpha", question="Question alpha?", top_k=3),
        WorkItem(item_id="item-b", query="beta", question="Question beta?", top_k=3),
    ]


def _runtime(
    store: RuntimeStore,
    provider,
    policy: ReplanPolicy = ReplanPolicy(),
    candidates: CandidateStore | None = None,
):
    return ResearchRuntime(
        search=_MultiDocSearch(total=3),
        provider=provider,
        store=store,
        source_namespace="fixture-corpus",
        policy=policy,
        candidate_store=candidates,
    )


class ReplanPolicyTests(unittest.TestCase):
    def test_default_policy_preserves_m1_3_terminal_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                runtime = _runtime(store, _PerDocLLM(broken_ids={"alpha-1"}))
                result = runtime.run(
                    session_id="s1",
                    items=[WorkItem(item_id="item-a", query="alpha", question="Q?", top_k=2)],
                    budget_requests=10,
                    observed_at=REASONED_AT,
                )
                # Both documents get requested, the second one breaks the
                # contract, and without a policy the item is terminal at
                # attempt 1 with its breadth untouched.
                self.assertEqual("completed", result["status"])
                self.assertEqual(1, result["items_rejected"])
                self.assertEqual([], result["degraded_items"])
                row = store.get_item("s1", "item-a")
                self.assertEqual(1, row["attempts"])
                self.assertEqual(2, row["effective_top_k"])
                self.assertEqual("citation_not_found", row["last_error"])
                self.assertEqual(2, result["requests_spent"])

    def test_retry_rejected_rescues_an_item_with_narrower_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                with CandidateStore(Path(tmp) / "candidates.db") as candidates:
                    # The second document breaks the contract; the first is
                    # fine. attempt 1 (top_k 2) fails on it, the degraded
                    # retry (top_k 1) never sees it and completes.
                    runtime = _runtime(
                        store,
                        _PerDocLLM(broken_ids={"alpha-1"}),
                        policy=ReplanPolicy(retry_rejected=True),
                        candidates=candidates,
                    )
                    result = runtime.run(
                        session_id="s1",
                        items=[WorkItem(item_id="item-a", query="alpha", question="Q?", top_k=2)],
                        budget_requests=10,
                        observed_at=REASONED_AT,
                    )
                    self.assertEqual("completed", result["status"])
                    self.assertEqual(1, result["items_completed"])
                    self.assertEqual(["item-a"], result["degraded_items"])
                    row = store.get_item("s1", "item-a")
                    self.assertEqual(2, row["attempts"])
                    self.assertEqual(1, row["effective_top_k"])
                    self.assertIsNone(row["last_error"])
                    self.assertEqual(3, result["requests_spent"])
                    self.assertEqual(
                        {"candidates": 1, "observations": 1, "distinct_canonical_keys": 1},
                        candidates.counts(),
                    )

    def test_retry_stops_at_max_attempts_and_min_top_k(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                runtime = _runtime(
                    store,
                    _PerDocLLM(broken_ids={"alpha-0", "alpha-1", "alpha-2"}),
                    policy=ReplanPolicy(retry_rejected=True, max_attempts=2),
                )
                result = runtime.run(
                    session_id="s1",
                    items=[WorkItem(item_id="item-a", query="alpha", question="Q?", top_k=2)],
                    budget_requests=10,
                    observed_at=REASONED_AT,
                )
                # attempt 1 at top_k 2 and attempt 2 at top_k 1 both reject;
                # max_attempts stops the cycle and the item is terminal.
                self.assertEqual(1, result["items_rejected"])
                row = store.get_item("s1", "item-a")
                self.assertEqual(2, row["attempts"])
                self.assertEqual(1, row["effective_top_k"])
                self.assertEqual(2, result["requests_spent"])

                # An item already at the floor never requeues at all.
                with RuntimeStore(Path(tmp) / "runtime2.db") as store2:
                    runtime2 = _runtime(
                        store2,
                        _PerDocLLM(broken_ids={"beta-0"}),
                        policy=ReplanPolicy(retry_rejected=True, max_attempts=3),
                    )
                    result2 = runtime2.run(
                        session_id="s2",
                        items=[WorkItem(item_id="item-b", query="beta", question="Q?", top_k=1)],
                        budget_requests=10,
                        observed_at=REASONED_AT,
                    )
                    self.assertEqual(1, result2["items_rejected"])
                    row2 = store2.get_item("s2", "item-b")
                    self.assertEqual(1, row2["attempts"])
                    self.assertEqual(1, row2["effective_top_k"])

    def test_degrade_to_fit_budget_reduces_largest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                with CandidateStore(Path(tmp) / "candidates.db") as candidates:
                    runtime = _runtime(
                        store,
                        _PerDocLLM(),
                        policy=ReplanPolicy(degrade_to_fit_budget=True),
                        candidates=candidates,
                    )
                    result = runtime.run(
                        session_id="s1",
                        items=_items(),
                        budget_requests=4,
                        observed_at=REASONED_AT,
                    )
                    # Worst case 6 > budget 4: both items degrade 3 -> 2 and
                    # the whole queue fits exactly.
                    self.assertEqual("completed", result["status"])
                    self.assertEqual(2, result["items_completed"])
                    self.assertEqual(["item-a", "item-b"], result["degraded_items"])
                    for item_id in ("item-a", "item-b"):
                        row = store.get_item("s1", item_id)
                        self.assertEqual(3, row["top_k"])
                        self.assertEqual(2, row["effective_top_k"])
                    self.assertEqual(4, result["requests_spent"])
                    self.assertEqual(4, candidates.counts()["candidates"])

    def test_degrade_respects_the_floor_and_budget_stop_remains_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                runtime = _runtime(
                    store,
                    _PerDocLLM(),
                    policy=ReplanPolicy(degrade_to_fit_budget=True),
                )
                result = runtime.run(
                    session_id="s1",
                    items=[
                        WorkItem(item_id="item-a", query="alpha", question="Q?", top_k=2),
                        WorkItem(item_id="item-b", query="beta", question="Q?", top_k=2),
                    ],
                    budget_requests=1,
                    observed_at=REASONED_AT,
                )
                # The queue degrades to the floor [1, 1] but still cannot fit
                # budget 1: the run proceeds and stops cleanly at the budget.
                self.assertEqual("budget_exhausted", result["status"])
                self.assertEqual(1, result["items_completed"])
                self.assertEqual(1, result["items_pending"])
                self.assertEqual(["item-a", "item-b"], result["degraded_items"])
                self.assertEqual(1, result["requests_spent"])
                self.assertEqual(1, store.get_item("s1", "item-a")["effective_top_k"])
                self.assertEqual(1, store.get_item("s1", "item-b")["effective_top_k"])

    def test_degrade_uses_remaining_budget_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                first = _runtime(store, _PerDocLLM())
                stopped = first.run(
                    session_id="s1",
                    items=_items(),
                    budget_requests=4,
                    observed_at=REASONED_AT,
                )
                # No policy on the first pass: item-a completes at top_k 3,
                # item-b is interrupted mid-item by the budget stop.
                self.assertEqual("budget_exhausted", stopped["status"])
                self.assertEqual(4, stopped["requests_spent"])
                self.assertEqual(3, store.get_item("s1", "item-b")["effective_top_k"])

                resumed = _runtime(
                    store,
                    _PerDocLLM(),
                    policy=ReplanPolicy(degrade_to_fit_budget=True),
                )
                result = resumed.run(
                    session_id="s1",
                    items=_items(),
                    budget_requests=6,
                    observed_at=REASONED_AT,
                )
                # Remaining budget 6 - 4 = 2 cannot cover item-b at top_k 3,
                # so it degrades to 2 and completes: 4 + 2 = 6 spent.
                self.assertEqual("completed", result["status"])
                self.assertEqual(["item-b"], result["degraded_items"])
                self.assertEqual(2, store.get_item("s1", "item-b")["effective_top_k"])
                self.assertEqual(6, result["requests_spent"])


if __name__ == "__main__":
    unittest.main()
