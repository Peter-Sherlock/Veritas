from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.extraction.store import CandidateStore
from veritas.providers.llm import LLMResponse
from veritas.runtime import (
    ResearchRuntime,
    RuntimeSessionError,
    RuntimeStore,
    RuntimeStoreError,
    WorkItem,
)
from veritas.search.provider import SearchResult, VersionedDocument


REASONED_AT = "2026-08-30T00:00:00Z"

DOCUMENTS = {
    "retries": "HTTPX retries connection setup failures.",
    "client": "HTTPX clients are created with httpx.Client().",
    "timeout": "Timeouts apply to every request by default.",
}

STATEMENTS = {
    "retries": "HTTPX retries connection setup failures",
    "client": "HTTPX clients are created with httpx.Client",
    "timeout": "Timeouts apply to every request by default",
}


def _document(doc_id: str, content: str) -> VersionedDocument:
    return VersionedDocument(
        doc_id=doc_id,
        version_id="1.0",
        title=doc_id,
        content=content,
        published_at="2026-08-01T00:00:00Z",
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


_CORPUS = {doc_id: _document(doc_id, content) for doc_id, content in DOCUMENTS.items()}


class _SingleDocSearch:
    """Serves one fixed document per query."""

    def search(self, query: str, *, top_k: int = 5, as_of: str | None = None):
        document = _CORPUS[query]
        return [
            SearchResult(
                doc_id=document.doc_id,
                version_id=document.version_id,
                title=document.title,
                path=Path("fixture.md"),
                score=1.0,
                snippet=document.content,
            )
        ][:top_k]

    def fetch(self, doc_id: str, version_id: str) -> VersionedDocument:
        if version_id != "1.0" or doc_id not in _CORPUS:
            raise KeyError((doc_id, version_id))
        return _CORPUS[doc_id]


class _ScriptedLLM:
    """Answers every document with one valid, source-grounded assertion."""

    def __init__(self) -> None:
        self.model_id = "scripted-model"
        self.calls: list[str] = []

    def complete(self, *, system: str, prompt: str, json_mode: bool = True):
        self.calls.append(prompt)
        document = json.loads(prompt)["document"]
        payload = {
            "assertions": [
                {
                    "statement": STATEMENTS[document["doc_id"]],
                    "relation": "supports",
                    "quote": document["content"],
                }
            ]
        }
        return LLMResponse(
            text=json.dumps(payload),
            model_id=self.model_id,
            prompt_tokens=10,
            completion_tokens=5,
        )


class _CrashAfterLLM(_ScriptedLLM):
    """Simulates a process crash after N successful provider calls."""

    def __init__(self, crash_after: int) -> None:
        super().__init__()
        self._crash_after = crash_after

    def complete(self, *, system: str, prompt: str, json_mode: bool = True):
        if len(self.calls) >= self._crash_after:
            raise RuntimeError("simulated process crash")
        return super().complete(system=system, prompt=prompt, json_mode=json_mode)


class _InvalidJSONForLLM(_ScriptedLLM):
    """Returns non-JSON for one doc_id, valid assertions otherwise."""

    def __init__(self, broken_doc_id: str) -> None:
        super().__init__()
        self._broken_doc_id = broken_doc_id

    def complete(self, *, system: str, prompt: str, json_mode: bool = True):
        if json.loads(prompt)["document"]["doc_id"] == self._broken_doc_id:
            return LLMResponse(text="{not json", model_id=self.model_id)
        return super().complete(system=system, prompt=prompt, json_mode=json_mode)


def _items() -> list[WorkItem]:
    return [
        WorkItem(
            item_id="q-retries", query="retries", question="What retry behavior is documented?"
        ),
        WorkItem(item_id="q-client", query="client", question="How are clients created?"),
        WorkItem(item_id="q-timeout", query="timeout", question="What about timeouts?"),
    ]


def _runtime(store: RuntimeStore, provider, candidates: CandidateStore | None = None):
    return ResearchRuntime(
        search=_SingleDocSearch(),
        provider=provider,
        store=store,
        source_namespace="fixture-corpus",
        candidate_store=candidates,
    )


class ResearchRuntimeTests(unittest.TestCase):
    def test_happy_path_completes_and_persists_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _ScriptedLLM()
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                with CandidateStore(Path(tmp) / "candidates.db") as candidates:
                    runtime = _runtime(store, provider, candidates)
                    result = runtime.run(
                        session_id="s1",
                        items=_items(),
                        budget_requests=10,
                        observed_at=REASONED_AT,
                    )
                    self.assertEqual("completed", result["status"])
                    self.assertEqual(3, result["items_completed"])
                    self.assertEqual(0, result["items_rejected"])
                    self.assertEqual(0, result["items_pending"])
                    # One request per item: each query retrieves one document.
                    self.assertEqual(3, result["requests_spent"])
                    self.assertEqual(10, result["budget_requests"])
                    self.assertEqual(3, len(provider.calls))
                    # One candidate per item, attributed to the session run.
                    self.assertEqual(
                        {"candidates": 3, "observations": 3, "distinct_canonical_keys": 3},
                        candidates.counts(),
                    )
                    observations = candidates.connection.execute(
                        "SELECT run_id FROM extraction_candidate_observations"
                    ).fetchall()
                    self.assertEqual(
                        {"session:s1"}, {row["run_id"] for row in observations}
                    )

    def test_budget_exhaustion_stops_cleanly_and_resumes_after_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _ScriptedLLM()
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                with CandidateStore(Path(tmp) / "candidates.db") as candidates:
                    runtime = _runtime(store, provider, candidates)
                    result = runtime.run(
                        session_id="s1",
                        items=_items()[:2],
                        budget_requests=1,
                        observed_at=REASONED_AT,
                    )
                    # The stop is clean, not an exception: the second item's
                    # reservation fails before any provider call is made.
                    self.assertEqual("budget_exhausted", result["status"])
                    self.assertEqual(1, result["requests_spent"])
                    self.assertEqual(1, result["items_completed"])
                    self.assertEqual(1, result["items_pending"])
                    self.assertEqual(1, len(provider.calls))

                    resumed = runtime.run(
                        session_id="s1",
                        items=_items()[:2],
                        budget_requests=5,
                        observed_at=REASONED_AT,
                    )
                    self.assertEqual("completed", resumed["status"])
                    self.assertEqual(2, resumed["requests_spent"])
                    self.assertEqual(2, len(provider.calls))
                    self.assertEqual(
                        {"candidates": 2, "observations": 2, "distinct_canonical_keys": 2},
                        candidates.counts(),
                    )

    def test_contract_rejection_is_recorded_and_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _InvalidJSONForLLM("client")
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                runtime = _runtime(store, provider)
                result = runtime.run(
                    session_id="s1",
                    items=_items(),
                    budget_requests=20,
                    observed_at=REASONED_AT,
                )
                self.assertEqual("completed", result["status"])
                self.assertEqual(2, result["items_completed"])
                self.assertEqual(1, result["items_rejected"])
                items = {item["item_id"]: item for item in store.list_items("s1")}
                self.assertEqual("invalid_json", items["q-client"]["last_error"])
                # A rejected item is terminal: re-running the session errors
                # out instead of silently re-spending the budget.
                with self.assertRaises(RuntimeSessionError) as caught:
                    runtime.run(
                        session_id="s1",
                        items=_items(),
                        budget_requests=20,
                        observed_at=REASONED_AT,
                    )
                self.assertEqual("session_completed", caught.exception.code)

    def test_crash_resume_skips_terminal_items_and_converges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            crashing = _CrashAfterLLM(crash_after=2)
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                with CandidateStore(Path(tmp) / "candidates.db") as candidates:
                    runtime = _runtime(store, crashing, candidates)
                    with self.assertRaises(RuntimeError):
                        runtime.run(
                            session_id="s1",
                            items=_items(),
                            budget_requests=20,
                            observed_at=REASONED_AT,
                        )
                    state = store.session_state("s1")
                    # Two items landed; the third was interrupted after its
                    # request was reserved: 2 + 1 = 3 spent.
                    self.assertEqual(3, state["requests_spent"])
                    items = {item["item_id"]: item for item in state["items"]}
                    self.assertEqual("completed", items["q-retries"]["status"])
                    self.assertEqual("completed", items["q-client"]["status"])
                    self.assertEqual("pending", items["q-timeout"]["status"])
                    self.assertEqual(1, items["q-timeout"]["attempts"])

                    # Resume with a healthy provider: terminal items are not
                    # re-extracted, the interrupted item is redone, and the
                    # candidate store stays duplicate-free.
                    healthy = _ScriptedLLM()
                    resumed_runtime = _runtime(store, healthy, candidates)
                    result = resumed_runtime.run(
                        session_id="s1",
                        items=_items(),
                        budget_requests=20,
                        observed_at=REASONED_AT,
                    )
                    self.assertEqual("completed", result["status"])
                    self.assertEqual(3, result["items_completed"])
                    self.assertEqual(1, len(healthy.calls))
                    self.assertEqual(4, result["requests_spent"])
                    self.assertEqual(3, candidates.counts()["candidates"])

                    # Reference run without the crash: identical terminal
                    # state (statuses and candidate identities).
                    with tempfile.TemporaryDirectory() as reference_tmp:
                        with RuntimeStore(
                            Path(reference_tmp) / "runtime.db"
                        ) as reference_store, CandidateStore(
                            Path(reference_tmp) / "candidates.db"
                        ) as reference_candidates:
                            reference = _runtime(
                                reference_store, _ScriptedLLM(), reference_candidates
                            )
                            reference.run(
                                session_id="s1",
                                items=_items(),
                                budget_requests=20,
                                observed_at=REASONED_AT,
                            )
                            self.assertEqual(
                                reference_store.session_state("s1")["status"],
                                store.session_state("s1")["status"],
                            )
                            self.assertEqual(
                                [i["status"] for i in reference_store.list_items("s1")],
                                [i["status"] for i in store.list_items("s1")],
                            )
                            reference_ids = {
                                row["candidate_id"]
                                for row in reference_candidates.connection.execute(
                                    "SELECT candidate_id FROM extraction_candidates"
                                )
                            }
                            resumed_ids = {
                                row["candidate_id"]
                                for row in candidates.connection.execute(
                                    "SELECT candidate_id FROM extraction_candidates"
                                )
                            }
                            self.assertEqual(reference_ids, resumed_ids)

    def test_resume_with_drifted_spec_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                runtime = _runtime(store, _ScriptedLLM())
                runtime.run(
                    session_id="s1",
                    items=_items()[:1],
                    budget_requests=5,
                    observed_at=REASONED_AT,
                )
                drifted = [
                    WorkItem(
                        item_id="q-retries",
                        query="retries",
                        question="A different question?",
                    )
                ]
                with self.assertRaises(RuntimeStoreError) as caught:
                    runtime.run(
                        session_id="s1",
                        items=drifted,
                        budget_requests=5,
                        observed_at=REASONED_AT,
                    )
                self.assertEqual("session_spec_drift", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
