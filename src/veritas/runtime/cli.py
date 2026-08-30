"""CLI for the research runtime: spec-driven sessions against live or replay providers.

The same command is rerun-safe: an interrupted session resumes from its
checkpoints, and a completed session simply re-prints its summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from veritas.extraction.store import CandidateStore
from veritas.providers.llm import FixtureLLM, OpenAICompatibleClient, RecordingLLM
from veritas.runtime.engine import ResearchRuntime, WorkItem
from veritas.runtime.store import RuntimeStore, RuntimeStoreError
from veritas.search.local_corpus import LocalCorpusProvider


_SPEC_ITEM_KEYS = {"item_id", "query", "question", "top_k", "as_of"}
_SPEC_ITEM_REQUIRED = {"item_id", "query", "question", "top_k"}


def _load_spec(path: str | Path) -> tuple[str, int, list[WorkItem]]:
    spec_path = Path(path)
    if not spec_path.is_file():
        raise ValueError(f"session spec not found: {spec_path}")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"session spec is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError("session spec must be a JSON object")
    session_id = spec.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session spec needs a non-empty string session_id")
    budget = spec.get("budget_requests")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise ValueError("session spec needs an integer budget_requests >= 1")
    raw_items = spec.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("session spec needs a non-empty items list")
    seen: set[str] = set()
    items: list[WorkItem] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict) or not _SPEC_ITEM_REQUIRED <= set(raw) <= _SPEC_ITEM_KEYS:
            raise ValueError(
                f"items[{index}] must contain {sorted(_SPEC_ITEM_REQUIRED)} "
                f"and at most {sorted(_SPEC_ITEM_KEYS)}"
            )
        item_id = raw["item_id"]
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError(f"items[{index}].item_id must be a non-empty string")
        if item_id in seen:
            raise ValueError(f"items[{index}] duplicates item_id {item_id!r}")
        seen.add(item_id)
        for field in ("query", "question"):
            if not isinstance(raw[field], str) or not raw[field].strip():
                raise ValueError(f"items[{index}].{field} must be a non-empty string")
        top_k = raw["top_k"]
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
            raise ValueError(f"items[{index}].top_k must be an integer >= 1")
        as_of = raw.get("as_of")
        if as_of is not None and (not isinstance(as_of, str) or not as_of.strip()):
            raise ValueError(f"items[{index}].as_of must be a non-empty string or null")
        items.append(
            WorkItem(
                item_id=item_id,
                query=raw["query"],
                question=raw["question"],
                top_k=top_k,
                as_of=as_of,
            )
        )
    return session_id, budget, items


def _session_summary(
    store: RuntimeStore,
    session_id: str,
    candidate_counts: dict[str, int] | None,
) -> dict[str, Any]:
    state = store.session_state(session_id)
    items = state["items"]
    summary: dict[str, Any] = {
        "session_id": session_id,
        "status": state["status"],
        "budget_requests": int(state["budget_requests"]),
        "requests_spent": int(state["requests_spent"]),
        "items_total": len(items),
        "items_completed": sum(1 for i in items if i["status"] == "completed"),
        "items_rejected": sum(1 for i in items if i["status"] == "rejected"),
        "items_pending": sum(1 for i in items if i["status"] == "pending"),
        "items": [
            {
                "item_id": item["item_id"],
                "query": item["query"],
                "question": item["question"],
                "top_k": int(item["top_k"]),
                "as_of": item["as_of"],
                "status": item["status"],
                "attempts": int(item["attempts"]),
                "last_error": item["last_error"],
            }
            for item in items
        ],
        "candidate_store": candidate_counts,
    }
    canonical = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    summary["content_hash"] = hashlib.sha256(canonical).hexdigest()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run or resume a research session through the extraction runtime"
    )
    parser.add_argument("--spec", required=True, help="session spec JSON path")
    parser.add_argument("--corpus-root", required=True, help="versioned corpus root")
    parser.add_argument("--runtime-store", required=True, help="SQLite path for session state")
    parser.add_argument(
        "--candidates-out", help="optional SQLite path; contract-valid candidates are persisted"
    )
    parser.add_argument(
        "--provider",
        choices=("live", "replay"),
        default="live",
        help="live calls an OpenAI-compatible API; replay deterministically re-runs a recording",
    )
    parser.add_argument("--model", default="deepseek-v4-flash", help="live provider model id")
    parser.add_argument(
        "--base-url", default="https://api.deepseek.com", help="live provider base URL"
    )
    parser.add_argument(
        "--record-out", help="live only; path for the recorded responses (crash-safe per item)"
    )
    parser.add_argument(
        "--record-in", help="replay only; recording produced by an earlier live run"
    )
    parser.add_argument(
        "--observed-at",
        help="ISO timestamp used for session checkpoints and evidence timestamps "
        "(defaults to the current UTC time)",
    )
    parser.add_argument("--output", help="optional path for the session summary JSON")
    args = parser.parse_args(argv)

    if args.provider == "live" and args.record_in:
        parser.error("--record-in belongs to --provider replay")
    if args.provider == "replay":
        if not args.record_in:
            parser.error("--provider replay requires --record-in")
        if args.record_out:
            parser.error("--record-out records live exchanges and is invalid with replay")

    try:
        session_id, budget, items = _load_spec(args.spec)
    except ValueError as exc:
        parser.error(str(exc))

    observed_at = args.observed_at or datetime.now(timezone.utc).isoformat()

    store = RuntimeStore(args.runtime_store)
    recorder: RecordingLLM | None = None
    candidates: CandidateStore | None = None
    try:
        if args.candidates_out:
            candidates = CandidateStore(args.candidates_out)
        existing = store.find_session(session_id)
        if existing is not None and existing["status"] == "completed":
            # Rerun-safe: a completed session re-prints its summary instead
            # of erroring, so the same command can be retried blindly.
            summary = _session_summary(
                store, session_id, candidates.counts() if candidates else None
            )
            _emit(summary, args.output)
            print(
                f"session {session_id!r} is already completed; summary reprinted",
                file=sys.stderr,
            )
            return 0

        if args.provider == "live":
            api_key = os.environ.get("VERITAS_LLM_API_KEY")
            if not api_key:
                parser.error(
                    "live provider requires the VERITAS_LLM_API_KEY environment variable"
                )
            # DeepSeek V4-Flash defaults to thinking mode; sessions pin
            # non-thinking for latency, cost and near-deterministic JSON.
            provider: Any = OpenAICompatibleClient(
                model=args.model,
                base_url=args.base_url,
                api_key=api_key,
                extra_payload={"thinking": {"type": "disabled"}},
            )
            if args.record_out:
                recorder = RecordingLLM(provider)
                provider = recorder
        else:
            provider = FixtureLLM.from_json(args.record_in)

        corpus = LocalCorpusProvider(args.corpus_root)
        runtime = ResearchRuntime(
            search=corpus,
            provider=provider,
            store=store,
            source_namespace=corpus.corpus_id,
            candidate_store=candidates,
        )

        item_total = len(items)
        item_done = 0

        def on_item_done(item: dict[str, Any]) -> None:
            nonlocal item_done
            item_done += 1
            if recorder is not None:
                recorder.save(args.record_out)
            spent = store.session_state(session_id)["requests_spent"]
            print(
                f"[session] {item_done}/{item_total} {item['item_id']} "
                f"{item['status']} requests={spent}",
                file=sys.stderr,
                flush=True,
            )

        try:
            result = runtime.run(
                session_id=session_id,
                items=items,
                budget_requests=budget,
                observed_at=observed_at,
                on_item_done=on_item_done,
            )
        except RuntimeStoreError as exc:
            parser.error(str(exc))
        if recorder is not None:
            recorder.save(args.record_out)
        print(
            f"research session: status={result['status']} "
            f"completed={result['items_completed']} rejected={result['items_rejected']} "
            f"pending={result['items_pending']} "
            f"requests={result['requests_spent']}/{result['budget_requests']}",
            file=sys.stderr,
        )
        summary = _session_summary(store, session_id, candidates.counts() if candidates else None)
        _emit(summary, args.output)
        return 0
    finally:
        if candidates is not None:
            candidates.close()
        store.close()


def _emit(summary: dict[str, Any], output: str | None) -> None:
    serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(serialized.encode("utf-8"))
    print(serialized, end="")


if __name__ == "__main__":
    raise SystemExit(main())
