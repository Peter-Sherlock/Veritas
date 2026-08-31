"""Autonomy CLI: one command for the autonomous research loop (M3-B)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from veritas.aggregation.store import ClaimClusterStore
from veritas.autonomy.watch import run_watch_loop
from veritas.extraction.store import CandidateStore
from veritas.providers.llm import FixtureLLM, OpenAICompatibleClient, RecordingLLM
from veritas.runtime.store import RuntimeStore
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one autonomous watch pass: apply real corpus drift, plan "
            "re-research for non-PASS conclusions, run a budgeted session "
            "and refresh the graph with its results"
        )
    )
    parser.add_argument("--corpus-root", required=True, help="versioned corpus root")
    parser.add_argument(
        "--evolution-store", required=True, help="SQLite path of the evolution repository"
    )
    parser.add_argument(
        "--runtime-store", required=True, help="SQLite path for research session state"
    )
    parser.add_argument(
        "--candidates-out",
        help="optional SQLite path; session candidates are persisted pre-aggregation",
    )
    parser.add_argument(
        "--cluster-store",
        help="optional SQLite path; claim identity clustering for re-research",
    )
    parser.add_argument(
        "--provider",
        choices=("live", "replay"),
        default="live",
        help="live calls an OpenAI-compatible API; replay re-runs a recording",
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
        "--session-id",
        help="research session id for this pass (defaults to watch-<observed_at>)",
    )
    parser.add_argument(
        "--observed-at",
        help="ISO timestamp for checkpoints and evidence (defaults to now UTC)",
    )
    parser.add_argument(
        "--project-id", default="watch-loop", help="project id for change events"
    )
    parser.add_argument(
        "--rule-version", default="p0-rules-2", help="rule version for assessments"
    )
    parser.add_argument("--output", help="optional path for the loop report JSON")
    args = parser.parse_args(argv)

    if args.provider == "live" and args.record_in:
        parser.error("--record-in belongs to --provider replay")
    if args.provider == "replay":
        if not args.record_in:
            parser.error("--provider replay requires --record-in")
        if args.record_out:
            parser.error("--record-out records live exchanges and is invalid with replay")

    observed_at = args.observed_at or datetime.now(timezone.utc).isoformat()
    session_id = args.session_id or f"watch-{observed_at}"

    repository = SQLiteRepository(args.evolution_store)
    runtime_store = RuntimeStore(args.runtime_store)
    clusters: ClaimClusterStore | None = None
    candidates: CandidateStore | None = None
    recorder: RecordingLLM | None = None
    try:
        if args.cluster_store:
            clusters = ClaimClusterStore(args.cluster_store)
        if args.candidates_out:
            candidates = CandidateStore(args.candidates_out)

        if args.provider == "live":
            api_key = os.environ.get("VERITAS_LLM_API_KEY")
            if not api_key:
                parser.error(
                    "live provider requires the VERITAS_LLM_API_KEY environment variable"
                )
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

        def on_item_done(_: dict[str, Any]) -> None:
            if recorder is not None and args.record_out:
                recorder.save(args.record_out)

        report = run_watch_loop(
            repository=repository,
            corpus=corpus,
            provider=provider,
            runtime_store=runtime_store,
            cluster_store=clusters,
            candidates_store=candidates,
            session_id=session_id,
            observed_at=observed_at,
            project_id=args.project_id,
            rule_version=args.rule_version,
            on_item_done=on_item_done,
        )
        if recorder is not None and args.record_out:
            recorder.save(args.record_out)

        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(serialized.encode("utf-8"))
        print(
            json.dumps(
                {
                    "session_id": report["session_id"],
                    "drift_applied": len(report["drift_applied"]),
                    "session_status": report["session"]["status"],
                    "requests_spent": report["session"]["requests_spent"],
                    "refreshes": len(report["refreshes"]),
                    "final_conclusion_outcomes": report["final_conclusion_outcomes"],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    finally:
        if clusters is not None:
            clusters.close()
        if candidates is not None:
            candidates.close()
        runtime_store.close()
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
