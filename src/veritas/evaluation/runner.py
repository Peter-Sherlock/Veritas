from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veritas.evaluation.metrics import evaluate_run
from veritas.evaluation.scenario import (
    build_change_package,
    canonical_json,
    initialize_t0,
    load_scenario,
)
from veritas.evidence.rules import evaluate_conclusion
from veritas.domain.models import EvolutionRun
from veritas.invalidation.repair import EvolutionEngine
from veritas.storage.sqlite import SQLiteRepository


@dataclass(frozen=True)
class RunnerResult:
    run: EvolutionRun
    metrics: dict[str, Any]
    artifact_dir: Path
    counts_after_first_run: dict[str, int]
    counts_after_replay: dict[str, int]


def run_scenario(
    scenario_path: str | Path,
    database_path: str | Path,
    artifacts_root: str | Path,
) -> RunnerResult:
    scenario = load_scenario(scenario_path)
    with SQLiteRepository(database_path) as repository:
        initialize_t0(repository, scenario)
        package = build_change_package(scenario)
        engine = EvolutionEngine(repository)

        first_run = engine.apply(package)
        counts_after_first = repository.entity_counts()
        replayed_run = engine.apply(package)
        counts_after_replay = repository.entity_counts()

        replay_determinism = first_run.to_dict() == replayed_run.to_dict()
        event_idempotency = counts_after_first == counts_after_replay
        current_outcomes = {
            conclusion.conclusion_key: conclusion.outcome.value
            for conclusion in repository.list_current_conclusions()
        }
        full_recompute_outcomes = _full_recompute(repository)
        provenance_errors = repository.validate_provenance()
        metrics = evaluate_run(
            first_run,
            scenario.ground_truth,
            current_outcomes=current_outcomes,
            full_recompute_outcomes=full_recompute_outcomes,
            replay_determinism=replay_determinism,
            event_idempotency=event_idempotency,
            provenance_errors=provenance_errors,
        )
        artifact_dir = write_artifacts(first_run, metrics, Path(artifacts_root))

    return RunnerResult(
        run=first_run,
        metrics=metrics,
        artifact_dir=artifact_dir,
        counts_after_first_run=counts_after_first,
        counts_after_replay=counts_after_replay,
    )


def _full_recompute(repository: SQLiteRepository) -> dict[str, str]:
    return {
        conclusion.conclusion_key: evaluate_conclusion(repository, conclusion).outcome.value
        for conclusion in repository.list_current_conclusions()
    }


def write_artifacts(run: EvolutionRun, metrics: dict[str, Any], artifacts_root: Path) -> Path:
    artifact_dir = artifacts_root / run.scenario_id / run.run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    common = {
        "scenario_id": run.scenario_id,
        "scenario_version": run.scenario_version,
        "run_id": run.run_id,
        "rule_version": run.rule_version,
        "input_snapshot_hash": run.input_snapshot_hash,
    }
    payloads = {
        "candidate_impact.json": {
            "change_event_id": run.change_event_id,
            **run.candidate_impact.to_dict(),
        },
        "confirmed_invalidations.json": {
            "reverification_results": list(run.reverification_results),
            "rechecked_unchanged": list(run.rechecked_unchanged),
            "confirmed_invalidations": list(run.confirmed_invalidations),
            "created_claims": list(run.created_claims),
            "created_claim_assessments": list(run.created_claim_assessments),
            "created_conclusions": list(run.created_conclusions),
            "untouched_nodes": list(run.untouched_nodes),
        },
        "conclusion_diff.json": (
            dict(run.conclusion_diffs[0])
            if len(run.conclusion_diffs) == 1
            else {"conclusion_diffs": list(run.conclusion_diffs)}
        ),
        "trace.json": {"events": list(run.trace_events)},
        "metrics.json": {"metrics": metrics, "operational_metrics": run.operational_metrics},
    }
    for filename, payload in payloads.items():
        _write_hashed_json(artifact_dir / filename, {**common, **payload})
    return artifact_dir


def _write_hashed_json(path: Path, payload: dict[str, Any]) -> None:
    without_hash = dict(payload)
    output_hash = hashlib.sha256(canonical_json(without_hash).encode("utf-8")).hexdigest()
    final_payload = {**without_hash, "output_hash": output_hash}
    path.write_text(
        json.dumps(final_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _default_paths() -> tuple[Path, Path, Path]:
    project_root = Path(__file__).resolve().parents[3]
    return (
        project_root / "datasets" / "scenarios" / "GS-001" / "scenario.json",
        project_root / "artifacts" / "GS-001" / "veritas.sqlite3",
        project_root / "artifacts",
    )


def main() -> int:
    default_scenario, default_database, default_artifacts = _default_paths()
    parser = argparse.ArgumentParser(description="Run a deterministic Veritas evolution scenario")
    parser.add_argument("--scenario", type=Path, default=default_scenario)
    parser.add_argument("--database", type=Path, default=default_database)
    parser.add_argument("--artifacts-root", type=Path, default=default_artifacts)
    args = parser.parse_args()

    result = run_scenario(args.scenario, args.database, args.artifacts_root)
    print(
        json.dumps(
            {
                "scenario_id": result.run.scenario_id,
                "run_id": result.run.run_id,
                "artifact_dir": str(result.artifact_dir),
                "metrics": result.metrics,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
