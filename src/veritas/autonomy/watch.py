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
from typing import Any

from veritas.autonomy.planner import plan_re_research
from veritas.autonomy.refresh import apply_research_refresh
from veritas.extraction.models import ExtractionCandidateBundle
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.integration import GraphBridge
from veritas.runtime.engine import ResearchRuntime, WorkItem
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


@dataclass(frozen=True)
class Drift:
    doc_id: str
    current_version: str
    latest_version: str


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
) -> dict[str, Any]:
    """One autonomous pass: drift -> plan -> research -> refresh."""
    bridge = GraphBridge(repository, corpus)
    engine = EvolutionEngine(repository)
    report: dict[str, Any] = {
        "session_id": session_id,
        "observed_at": observed_at,
        "drift_applied": [],
        "plan": None,
        "session": None,
        "refreshes": [],
    }

    for drift in detect_drift(repository, corpus):
        event, new_source = bridge.revision_event(
            drift.doc_id,
            drift.current_version,
            drift.latest_version,
            project_id=project_id,
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

    plan = plan_re_research(
        repository,
        session_id=session_id,
        top_k=top_k,
        requests_per_item=requests_per_item,
    )
    report["plan"] = plan.to_spec()

    bundles: list[ExtractionCandidateBundle] = []
    if plan.items:
        runtime = ResearchRuntime(
            search=corpus,
            provider=provider,
            store=runtime_store,
            source_namespace=corpus.corpus_id,
            candidate_store=candidates_store,
            cluster_store=cluster_store,
        )
        result = runtime.run(
            session_id=session_id,
            items=[WorkItem(**item.to_spec()) for item in plan.items],
            budget_requests=plan.budget_requests,
            observed_at=observed_at,
            on_item_bundle=bundles.append,
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
            "budget_requests": plan.budget_requests,
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

    for bundle in bundles:
        report["refreshes"].append(
            apply_research_refresh(
                repository,
                bundle=bundle,
                session_id=session_id,
                rule_version=rule_version,
                refreshed_at=observed_at,
            )
        )

    report["final_conclusion_outcomes"] = {
        conclusion.conclusion_key: conclusion.outcome.value
        for conclusion in repository.list_current_conclusions()
    }
    return report
