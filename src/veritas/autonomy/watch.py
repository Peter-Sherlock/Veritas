"""Watch mode: detect corpus drift and run the autonomous loop (M3-B).

The loop in one command:

1. **Drift** — compare the evolution store's active source versions with
   the corpus manifest's latest versions; every real content change
   becomes a bridge ``revise`` ChangeEvent applied with *no* new claims,
   so affected claims lose support and their conclusions flip to unknown
   (the engine's honest "source changed, re-research pending" state);
2. **Plan** — the M3-1 planner turns every non-PASS conclusion into
   runtime session items;
3. **Research** — a real runtime session (budget, checkpoints,
   clustering) executes the plan; the engine's ``on_item_bundle`` hook
   hands the loop each resolved candidate bundle;
4. **Refresh** — the M3-2 applier re-enters each bundle into the graph,
   repairing claims whose facts still hold and recomputing conclusions.

Drift detection never invents revisions: identical content between the
registered version and the corpus latest is skipped, so the SAME-step
semantics of the M1-5B benchmark carry over to live watching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from veritas.autonomy.planner import plan_re_research
from veritas.autonomy.refresh import apply_research_refresh
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.integration import GraphBridge
from veritas.runtime.engine import ResearchRuntime, WorkItem
from veritas.runtime.store import SESSION_COMPLETED
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


_WATCH_CONTEXT = "autonomy-watch-1"


@dataclass(frozen=True)
class Drift:
    doc_id: str
    current_version: str
    latest_version: str


class WatchLoopError(ValueError):
    """A stable orchestration failure at the durable watch boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def detect_drift(
    repository: SQLiteRepository, corpus: LocalCorpusProvider
) -> list[Drift]:
    """Registered sources whose content the corpus has genuinely replaced."""
    current_by_source: dict[str, Any] = {}
    for source in repository.list_source_versions():
        if repository.source_is_active(source.version_id):
            current_by_source.setdefault(source.source_id, source)
    prefix = f"{corpus.corpus_id}:"
    drifts: list[Drift] = []
    for source_id, source in sorted(current_by_source.items()):
        if not source_id.startswith(prefix):
            continue
        doc_id = source_id[len(prefix) :]
        try:
            latest = corpus.latest_version(doc_id)
        except KeyError:
            # The corpus no longer carries this document; nothing to
            # revise against.
            continue
        if latest == source.version_label:
            continue
        if corpus.fetch(doc_id, latest).content_hash == source.content_hash:
            # Same content under a new label is not a revision.
            continue
        drifts.append(
            Drift(
                doc_id=doc_id,
                current_version=source.version_label,
                latest_version=latest,
            )
        )
    return sorted(drifts, key=lambda drift: drift.doc_id)


def _snapshot_hash(repository: SQLiteRepository) -> str:
    import hashlib
    import json

    state = {
        "claims": [claim.to_dict() for claim in repository.list_claims()],
        "edges": [edge.to_dict() for edge in repository.list_dependency_edges()],
    }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _drain_item_outputs(
    *,
    repository: SQLiteRepository,
    runtime_store: Any,
    session_id: str,
    rule_version: str,
    delivered_at: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deliver a session outbox at least once; refresh application is idempotent."""
    refreshes: list[dict[str, Any]] = []
    ignored: list[str] = []
    for output in runtime_store.list_item_outputs(session_id):
        status = output["delivery_status"]
        if status == "ignored":
            ignored.append(output["item_id"])
            continue
        if status == "applied":
            payload = repository.find_research_refresh(output["refresh_id"])
            if payload is None:
                raise WatchLoopError(
                    "missing_applied_refresh",
                    f"runtime output {session_id!r}/{output['item_id']!r} is marked "
                    "applied but its evolution refresh is missing",
                )
            refreshes.append(payload)
            continue

        bundle = output["bundle"]
        if not bundle.claims and not bundle.evidence_spans and not bundle.edges:
            runtime_store.mark_item_output_delivered(
                session_id,
                output["item_id"],
                delivery_status="ignored",
                delivered_at=delivered_at,
                refresh_id=None,
            )
            ignored.append(output["item_id"])
            continue

        payload = apply_research_refresh(
            repository,
            bundle=bundle,
            session_id=session_id,
            rule_version=rule_version,
            refreshed_at=delivered_at,
        )
        # Evolution commits first. If acknowledgement crashes, the still-pending
        # outbox row is retried and the deterministic refresh id returns payload.
        runtime_store.mark_item_output_delivered(
            session_id,
            output["item_id"],
            delivery_status="applied",
            delivered_at=delivered_at,
            refresh_id=payload["refresh_id"],
        )
        refreshes.append(payload)
    return refreshes, ignored


def _store_path(store: Any) -> str | None:
    if store is None:
        return None
    return str(store.database_path.resolve())


def _watch_context(
    *,
    corpus: LocalCorpusProvider,
    provider: Any,
    cluster_store: Any,
    candidates_store: Any,
    project_id: str,
    rule_version: str,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "context_version": _WATCH_CONTEXT,
        "corpus_id": corpus.corpus_id,
        "provider_model_id": provider.model_id,
        "cluster_store": _store_path(cluster_store),
        "candidates_store": _store_path(candidates_store),
        "project_id": project_id,
        "rule_version": rule_version,
        "observed_at": observed_at,
    }


def run_watch_loop(
    *,
    repository: SQLiteRepository,
    corpus: LocalCorpusProvider,
    provider: Any,
    runtime_store: Any,
    cluster_store: Any,
    session_id: str,
    observed_at: str,
    project_id: str,
    rule_version: str,
    candidates_store: Any = None,
    top_k: int = 3,
    requests_per_item: int = 3,
    on_item_done: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """One autonomous pass: drift -> plan -> research -> refresh."""
    bridge = GraphBridge(repository, corpus)
    engine = EvolutionEngine(repository)
    requested_context = _watch_context(
        corpus=corpus,
        provider=provider,
        cluster_store=cluster_store,
        candidates_store=candidates_store,
        project_id=project_id,
        rule_version=rule_version,
        observed_at=observed_at,
    )
    effective_observed_at = observed_at
    report: dict[str, Any] = {
        "session_id": session_id,
        "observed_at": observed_at,
        "drift_applied": [],
        "plan": None,
        "session": None,
        "refreshes": [],
        "ignored_outputs": [],
    }

    existing_session = runtime_store.find_session(session_id)
    if existing_session is not None:
        stored_context = runtime_store.get_session_context(session_id, _WATCH_CONTEXT)
        if stored_context is None:
            stored_context = runtime_store.bind_session_context(
                session_id,
                _WATCH_CONTEXT,
                requested_context,
                bound_at=observed_at,
            )
        stable_context = {key: value for key, value in requested_context.items() if key != "observed_at"}
        stable_stored = {key: value for key, value in stored_context.items() if key != "observed_at"}
        if stable_context != stable_stored:
            raise WatchLoopError(
                "session_context_drift",
                f"session {session_id!r} must resume with its original watch context",
            )
        effective_observed_at = str(stored_context["observed_at"])
        recovered, ignored = _drain_item_outputs(
            repository=repository,
            runtime_store=runtime_store,
            session_id=session_id,
            rule_version=rule_version,
            delivered_at=effective_observed_at,
        )
        report["refreshes"].extend(recovered)
        report["ignored_outputs"].extend(ignored)

    drifts = detect_drift(repository, corpus)
    if existing_session is not None and drifts:
        raise WatchLoopError(
            "session_world_drift",
            f"session {session_id!r} already exists but the corpus changed again; "
            "finish recovery and use a new session id for the new world state",
        )

    for drift in drifts:
        event, new_source = bridge.revision_event(
            drift.doc_id,
            drift.current_version,
            drift.latest_version,
            project_id=project_id,
            observed_at=effective_observed_at,
        )
        engine.apply(
            ChangePackage(
                scenario_id="watch",
                scenario_version="1.0.0",
                input_snapshot_id=f"watch:{session_id}",
                input_snapshot_hash=_snapshot_hash(repository),
                rule_version=rule_version,
                event=event,
                new_source=new_source,
                new_claims=(),
                new_evidence=(),
                new_edges=(),
            )
        )
        report["drift_applied"].append(
            {
                "change_event_id": event.change_event_id,
                "doc_id": drift.doc_id,
                "old_version": drift.current_version,
                "new_version": drift.latest_version,
            }
        )

    if existing_session is None:
        plan = plan_re_research(
            repository,
            session_id=session_id,
            top_k=top_k,
            requests_per_item=requests_per_item,
        )
        plan_spec = plan.to_spec()
    else:
        plan_spec = runtime_store.session_spec(session_id)
    report["plan"] = plan_spec

    if plan_spec["items"]:
        if existing_session is None:
            runtime_store.create_session(
                session_id=session_id,
                items=plan_spec["items"],
                budget_requests=int(plan_spec["budget_requests"]),
                observed_at=effective_observed_at,
            )
            runtime_store.bind_session_context(
                session_id,
                _WATCH_CONTEXT,
                requested_context,
                bound_at=effective_observed_at,
            )
        runtime = ResearchRuntime(
            search=corpus,
            provider=provider,
            store=runtime_store,
            source_namespace=corpus.corpus_id,
            candidate_store=candidates_store,
            cluster_store=cluster_store,
        )
        current = runtime_store.find_session(session_id)
        if current is not None and current["status"] == SESSION_COMPLETED:
            result = runtime.result(session_id)
        else:
            result = runtime.run(
                session_id=session_id,
                items=[WorkItem(**item) for item in plan_spec["items"]],
                budget_requests=int(plan_spec["budget_requests"]),
                observed_at=effective_observed_at,
                on_item_done=on_item_done,
            )
    else:
        # Nothing to re-research: skip the session entirely (the runtime
        # rejects empty sessions by contract).
        result = {
            "status": "completed",
            "items_completed": 0,
            "items_rejected": 0,
            "items_pending": 0,
            "requests_spent": 0,
            "budget_requests": int(plan_spec["budget_requests"]),
        }
    report["session"] = {
        key: result[key]
        for key in (
            "status",
            "items_completed",
            "items_rejected",
            "items_pending",
            "requests_spent",
            "budget_requests",
        )
    }

    if runtime_store.find_session(session_id) is not None:
        delivered, ignored = _drain_item_outputs(
            repository=repository,
            runtime_store=runtime_store,
            session_id=session_id,
            rule_version=rule_version,
            delivered_at=effective_observed_at,
        )
        known_refresh_ids = {item["refresh_id"] for item in report["refreshes"]}
        report["refreshes"].extend(
            item for item in delivered if item["refresh_id"] not in known_refresh_ids
        )
        report["ignored_outputs"] = sorted(
            set(report["ignored_outputs"]) | set(ignored)
        )
        outputs = runtime_store.list_item_outputs(session_id)
        report["outbox"] = {
            "outputs": len(outputs),
            "pending": sum(
                1 for output in outputs if output["delivery_status"] == "pending"
            ),
            "applied": sum(
                1 for output in outputs if output["delivery_status"] == "applied"
            ),
            "ignored": sum(
                1 for output in outputs if output["delivery_status"] == "ignored"
            ),
        }
    else:
        report["outbox"] = {
            "outputs": 0, "pending": 0, "applied": 0, "ignored": 0
        }

    report["final_conclusion_outcomes"] = {
        conclusion.conclusion_key: conclusion.outcome.value
        for conclusion in repository.list_current_conclusions()
    }
    return report
