"""Research runtime engine: work queue processing, checkpoints and budget.

The engine turns a queue of research items into checkpointed session state.
It reuses the calibrated extraction pipeline unchanged; the request budget
is enforced by wrapping the provider, so the probabilistic boundary stays
exactly the one that was calibrated in M1-2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from veritas.extraction.models import ExtractionContractError
from veritas.extraction.pipeline import ResearchExtractionPipeline
from veritas.extraction.store import CandidateStore, candidates_from_document
from veritas.runtime.store import (
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    RuntimeStore,
)


class BudgetExhausted(Exception):
    """The session budget is fully reserved; no further request may start."""


class RuntimeSessionError(ValueError):
    """A stable, classifiable failure at the session boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class WorkItem:
    """One research question in the session queue."""

    item_id: str
    query: str
    question: str
    top_k: int = 3
    as_of: str | None = None

    def to_spec(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "query": self.query,
            "question": self.question,
            "top_k": self.top_k,
            "as_of": self.as_of,
        }


@dataclass(frozen=True)
class ReplanPolicy:
    """Deterministic replanning policy for a session (D-036).

    Degradation is the only retry axis. A contract rejection means the
    model's answer for a specific document violated the contract, so
    retrying the exact same prompt would deterministically reproduce the
    rejection (fixture replay) or waste budget (live variance); retrying
    with a narrower retrieval changes the prompts instead. Likewise,
    budget pressure is resolved before the run by degrading the queue to
    fit the remaining budget, not by hoping to squeeze through.
    """

    retry_rejected: bool = False
    degrade_to_fit_budget: bool = False
    max_attempts: int = 2
    min_top_k: int = 1


@dataclass(frozen=True)
class _RejectDecision:
    requeue: bool
    new_top_k: int


class _BudgetedProvider:
    """Reserves one budget unit before every provider call (D-034).

    The reservation is persisted before the inner provider is invoked, so a
    crash after reserving but before a response still counts the request as
    spent: the budget can be underspent across a crash, never overspent.
    """

    def __init__(self, store: RuntimeStore, inner: Any) -> None:
        self._store = store
        self._inner = inner
        self._session_id: str | None = None

    def bind(self, session_id: str) -> None:
        self._session_id = session_id

    def complete(self, *, system: str, prompt: str, json_mode: bool = True):
        if self._session_id is None:
            raise RuntimeSessionError(
                "session_not_bound", "the runtime provider was used before a session was bound"
            )
        if not self._store.try_reserve_request(self._session_id):
            raise BudgetExhausted(
                f"session {self._session_id!r} has exhausted its request budget"
            )
        return self._inner.complete(system=system, prompt=prompt, json_mode=json_mode)


class ResearchRuntime:
    """Process a work queue with per-item checkpoints and a request budget.

    Resume semantics: re-opening a session with the same spec skips items
    already in a terminal state (``completed``/``rejected``) and continues
    the pending ones. Redone items are safe against the candidate store's
    identity dedup (D-032) — an item whose candidates were persisted before
    a crash contributes no duplicates when re-extracted.

    With a :class:`ReplanPolicy`, two deterministic replans become active:
    a rejected item is requeued once with a narrower retrieval (the
    degraded breadth is persisted, so retries survive crashes), and a queue
    that cannot fit the remaining budget is degraded to fit before the run
    starts.
    """

    def __init__(
        self,
        *,
        search: Any,
        provider: Any,
        store: RuntimeStore,
        source_namespace: str,
        candidate_store: CandidateStore | None = None,
        policy: ReplanPolicy = ReplanPolicy(),
    ) -> None:
        self._store = store
        self._candidate_store = candidate_store
        self._source_namespace = source_namespace
        self._policy = policy
        self._budgeted = _BudgetedProvider(store, provider)
        self._pipeline = ResearchExtractionPipeline(
            search,
            self._budgeted,
            source_namespace=source_namespace,
        )

    def run(
        self,
        *,
        session_id: str,
        items: Sequence[WorkItem],
        budget_requests: int,
        observed_at: str,
        on_item_done: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run or resume the session until it lands or the budget stops it.

        Budget exhaustion is a clean stop, not an error: the session flips
        to ``budget_exhausted`` with pending items intact, and raising
        ``budget_requests`` on a later call resumes them. ``on_item_done``
        fires after every item reaches a terminal state (and once when the
        budget stops mid-item), receiving the updated work-item row — the
        hook for progress streaming and crash-safe recording.
        """
        self._budgeted.bind(session_id)
        self._store.create_session(
            session_id=session_id,
            items=[item.to_spec() for item in items],
            budget_requests=budget_requests,
            observed_at=observed_at,
        )
        if self._store.session_state(session_id)["status"] == SESSION_COMPLETED:
            raise RuntimeSessionError(
                "session_completed",
                f"session {session_id!r} is already completed; "
                "open a new session to retry its questions",
            )
        if self._policy.degrade_to_fit_budget:
            state = self._store.session_state(session_id)
            available = int(state["budget_requests"]) - int(state["requests_spent"])
            self._store.degrade_queue_to_fit(
                session_id,
                available,
                min_top_k=self._policy.min_top_k,
                observed_at=observed_at,
            )
        for item in self._store.pending_items(session_id):
            item_id = item["item_id"]
            while True:
                row = self._store.get_item(session_id, item_id)
                self._store.start_item(session_id, item_id)
                try:
                    bundle = self._pipeline.run(
                        query=item["query"],
                        question=item["question"],
                        reasoned_at=observed_at,
                        top_k=int(row["effective_top_k"]),
                        as_of=item["as_of"],
                    )
                except BudgetExhausted:
                    self._store.mark_session_budget_exhausted(session_id, observed_at)
                    self._notify(on_item_done, session_id, item_id)
                    return self._result(session_id)
                except ExtractionContractError as exc:
                    decision = self._decide_reject_retry(session_id, item_id)
                    if decision.requeue:
                        # Degrade-and-retry: the narrowed breadth is
                        # persisted, so the retry survives a crash.
                        self._store.requeue_item(session_id, item_id, decision.new_top_k)
                        continue
                    self._store.mark_item_rejected(
                        session_id, item_id, exc.code, observed_at
                    )
                else:
                    if self._candidate_store is not None:
                        records = [
                            record
                            for document in bundle.documents
                            for record in candidates_from_document(
                                document, source_namespace=self._source_namespace
                            )
                        ]
                        self._candidate_store.persist(
                            records,
                            run_id=f"session:{session_id}",
                            observed_at=observed_at,
                        )
                    self._store.mark_item_completed(session_id, item_id, observed_at)
                self._notify(on_item_done, session_id, item_id)
                break
        self._store.mark_session_completed(session_id, observed_at)
        return self._result(session_id)

    def _decide_reject_retry(self, session_id: str, item_id: str) -> _RejectDecision:
        row = self._store.get_item(session_id, item_id)
        attempts = int(row["attempts"])
        effective = int(row["effective_top_k"])
        if (
            self._policy.retry_rejected
            and attempts < self._policy.max_attempts
            and effective > self._policy.min_top_k
        ):
            return _RejectDecision(requeue=True, new_top_k=effective - 1)
        return _RejectDecision(requeue=False, new_top_k=effective)

    def _notify(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
        session_id: str,
        item_id: str,
    ) -> None:
        if callback is not None:
            callback(self._store.get_item(session_id, item_id))

    def _result(self, session_id: str) -> dict[str, Any]:
        state = self._store.session_state(session_id)
        items = state["items"]
        return {
            "session_id": session_id,
            "status": state["status"],
            "items_total": len(items),
            "items_completed": sum(1 for i in items if i["status"] == "completed"),
            "items_rejected": sum(1 for i in items if i["status"] == "rejected"),
            "items_pending": sum(1 for i in items if i["status"] == "pending"),
            "degraded_items": [
                i["item_id"]
                for i in items
                if int(i["effective_top_k"]) < int(i["top_k"])
            ],
            "requests_spent": int(state["requests_spent"]),
            "budget_requests": int(state["budget_requests"]),
        }


__all__ = [
    "BudgetExhausted",
    "ReplanPolicy",
    "ResearchRuntime",
    "RuntimeSessionError",
    "WorkItem",
    "SESSION_ACTIVE",
    "SESSION_COMPLETED",
]
