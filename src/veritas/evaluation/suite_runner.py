from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veritas.evaluation.runner import RunnerResult, _write_hashed_json, run_scenario
from veritas.evaluation.scenario import Scenario, canonical_json, load_scenario


FAILURE_CODES = (
    "F01_IMPACT_DETECTION",
    "F02_INVALIDATION_DECISION",
    "F03_REPAIR_CORRECTNESS",
    "F04_RECOMPUTE_SCOPE",
    "F05_PROVENANCE_INTEGRITY",
    "F06_REPLAY_REPRODUCIBILITY",
)
EXPECTED_RUN_ARTIFACTS = {
    "candidate_impact.json",
    "confirmed_invalidations.json",
    "conclusion_diff.json",
    "trace.json",
    "metrics.json",
}


@dataclass(frozen=True)
class SuiteEntry:
    scenario: Scenario
    ground_truth_hash: str


@dataclass(frozen=True)
class SuiteRunnerResult:
    summary: dict[str, Any]
    summary_path: Path
    scenario_results: tuple[RunnerResult, ...]


def run_suite(
    manifest_path: str | Path,
    artifacts_root: str | Path,
) -> SuiteRunnerResult:
    manifest_file = Path(manifest_path).resolve()
    manifest, entries = _load_manifest(manifest_file)
    suite_key = f"{manifest['suite_id']}-{manifest['suite_version']}"
    suite_dir = Path(artifacts_root).resolve() / "suites" / suite_key
    run_artifacts_root = suite_dir / "runs"

    scenario_results: list[RunnerResult] = []
    per_scenario: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []

    # Each scenario starts from an independent empty database. The SQLite files
    # are deliberately temporary so a prior idempotent run cannot mask a code change.
    with tempfile.TemporaryDirectory(prefix="veritas-suite-") as database_root:
        for entry in entries:
            scenario = entry.scenario
            result = run_scenario(
                scenario.path,
                Path(database_root) / f"{scenario.scenario_id}.sqlite3",
                run_artifacts_root,
            )
            scenario_results.append(result)
            artifact_errors = verify_artifact_directory(result.artifact_dir)
            if artifact_errors:
                failures.append(_artifact_failure(result, artifact_errors))
            failures.extend(result.metrics["failures"])
            per_scenario[scenario.scenario_id] = {
                "scenario_version": scenario.scenario_version,
                "rule_version": scenario.rule_version,
                "ground_truth_hash": entry.ground_truth_hash,
                "run_id": result.run.run_id,
                "artifact_dir": result.artifact_dir.relative_to(suite_dir).as_posix(),
                "artifact_hash_valid": not artifact_errors,
                "recomputed_conclusions": len(result.run.recomputed_conclusions),
                "total_conclusions": int(scenario.ground_truth["total_conclusions"]),
                "metrics": result.metrics,
            }

    aggregate = _aggregate(entries, scenario_results)
    taxonomy_counts = {code: 0 for code in FAILURE_CODES}
    for failure in failures:
        taxonomy_counts[failure["failure_code"]] += 1

    # Suites may declare their acceptance contract in the manifest. Manifests
    # without an "acceptance" block keep the frozen P0-2B behavior unchanged.
    acceptance_spec = manifest.get("acceptance")
    if acceptance_spec:
        evaluation_status = acceptance_spec["evaluation_status"]
        gate_p0_decision = acceptance_spec["gate_p0_decision"]
        acceptance_field = acceptance_spec["field"]
    else:
        evaluation_status = "P0-2B_IMPLEMENTATION_VERIFICATION"
        gate_p0_decision = "not_evaluated_in_p0_2b"
        acceptance_field = "p0_2b_acceptance_candidate"
    acceptance_candidate = _is_acceptance_candidate(
        scenario_results,
        aggregate,
        failures,
        acceptance_spec,
    )
    summary = {
        "suite_id": manifest["suite_id"],
        "suite_version": manifest["suite_version"],
        "manifest_hash": hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest(),
        "evaluation_status": evaluation_status,
        "gate_p0_decision": gate_p0_decision,
        "scenario_order": [entry.scenario.scenario_id for entry in entries],
        "per_scenario": per_scenario,
        "macro_average": aggregate["macro_average"],
        "micro_aggregate": aggregate["micro_aggregate"],
        "recompute_totals": aggregate["recompute_totals"],
        "failure_taxonomy_counts": taxonomy_counts,
        "critical_failure_count": len(failures),
        "failures": failures,
        acceptance_field: acceptance_candidate,
    }
    summary_path = suite_dir / "summary.json"
    _write_hashed_json(summary_path, summary)
    return SuiteRunnerResult(
        summary=summary,
        summary_path=summary_path,
        scenario_results=tuple(scenario_results),
    )


def verify_artifact_directory(artifact_dir: str | Path) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    artifact_path = Path(artifact_dir)
    present = {path.name for path in artifact_path.glob("*.json")}
    for filename in sorted(EXPECTED_RUN_ARTIFACTS - present):
        errors.append({"artifact": filename, "error": "missing_artifact"})
    for path in sorted(artifact_path.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"artifact": path.name, "error": f"unreadable_json: {exc}"})
            continue
        expected_hash = payload.pop("output_hash", None)
        actual_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            errors.append(
                {
                    "artifact": path.name,
                    "error": "output_hash_mismatch",
                    "expected": str(expected_hash),
                    "actual": actual_hash,
                }
            )
    return errors


def _load_manifest(manifest_path: Path) -> tuple[dict[str, Any], tuple[SuiteEntry, ...]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("suite_id") or not manifest.get("suite_version"):
        raise ValueError("suite manifest requires suite_id and suite_version")
    raw_entries = manifest.get("scenarios")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("suite manifest requires an explicit non-empty scenarios list")

    entries: list[SuiteEntry] = []
    seen_ids: set[str] = set()
    for raw_entry in raw_entries:
        scenario_path = (manifest_path.parent / raw_entry["path"]).resolve()
        scenario = load_scenario(scenario_path)
        expected = {
            "scenario_id": scenario.scenario_id,
            "scenario_version": scenario.scenario_version,
            "rule_version": scenario.rule_version,
            "ground_truth_hash": scenario.ground_truth_hash,
        }
        actual = {key: raw_entry.get(key) for key in expected}
        if actual != expected:
            raise ValueError(
                f"suite manifest entry does not match {scenario_path}: "
                f"expected {expected}, got {actual}"
            )
        if scenario.scenario_id in seen_ids:
            raise ValueError(f"duplicate scenario in suite manifest: {scenario.scenario_id}")
        seen_ids.add(scenario.scenario_id)
        entries.append(
            SuiteEntry(
                scenario=scenario,
                ground_truth_hash=scenario.ground_truth_hash,
            )
        )
    return manifest, tuple(entries)


def _aggregate(
    entries: tuple[SuiteEntry, ...],
    results: list[RunnerResult],
) -> dict[str, Any]:
    metric_names = (
        "candidate_impact_precision",
        "candidate_impact_recall",
        "invalidation_precision",
        "invalidation_recall",
        "unaffected_preservation",
        "selective_recompute_ratio",
        "full_recompute_ratio",
    )
    macro_average = {
        name: sum(float(result.metrics[name]) for result in results) / len(results)
        for name in metric_names
    }
    for name in (
        "repair_success",
        "full_recompute_equivalent",
        "replay_determinism",
        "event_idempotency",
        "provenance_integrity",
    ):
        macro_average[f"{name}_rate"] = (
            sum(bool(result.metrics[name]) for result in results) / len(results)
        )

    actual_candidate: set[str] = set()
    expected_candidate: set[str] = set()
    actual_confirmed: set[str] = set()
    expected_confirmed: set[str] = set()
    actual_untouched: set[str] = set()
    expected_untouched: set[str] = set()
    recomputed_total = 0
    conclusion_total = 0
    for entry, result in zip(entries, results, strict=True):
        prefix = f"{entry.scenario.scenario_id}:"
        actual_candidate.update(
            prefix + node
            for node in (*result.run.candidate_impact.claims, *result.run.candidate_impact.conclusions)
        )
        expected_candidate.update(prefix + node for node in entry.scenario.ground_truth["reverify"])
        actual_confirmed.update(
            prefix + item["node_key"] for item in result.run.confirmed_invalidations
        )
        expected_confirmed.update(
            prefix + node for node in entry.scenario.ground_truth["semantic_change"]
        )
        actual_untouched.update(prefix + node for node in result.run.untouched_nodes)
        expected_untouched.update(prefix + node for node in entry.scenario.ground_truth["untouched"])
        recomputed_total += len(result.run.recomputed_conclusions)
        conclusion_total += int(entry.scenario.ground_truth["total_conclusions"])

    micro_aggregate = {
        "candidate_impact_precision": _precision(actual_candidate, expected_candidate),
        "candidate_impact_recall": _recall(actual_candidate, expected_candidate),
        "invalidation_precision": _precision(actual_confirmed, expected_confirmed),
        "invalidation_recall": _recall(actual_confirmed, expected_confirmed),
        "unaffected_preservation": _recall(actual_untouched, expected_untouched),
    }
    recompute_totals = {
        "selective_recomputed_conclusions": recomputed_total,
        "selective_total_conclusions": conclusion_total,
        "selective_recompute_ratio": (
            recomputed_total / conclusion_total if conclusion_total else 0.0
        ),
        "full_recomputed_conclusions": conclusion_total,
        "full_total_conclusions": conclusion_total,
        "full_recompute_ratio": 1.0 if conclusion_total else 0.0,
    }
    return {
        "macro_average": macro_average,
        "micro_aggregate": micro_aggregate,
        "recompute_totals": recompute_totals,
    }


def _precision(actual: set[str], expected: set[str]) -> float:
    if not actual:
        return 1.0 if not expected else 0.0
    return len(actual & expected) / len(actual)


def _recall(actual: set[str], expected: set[str]) -> float:
    if not expected:
        return 1.0
    return len(actual & expected) / len(expected)


def _artifact_failure(
    result: RunnerResult,
    artifact_errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "failure_code": "F05_PROVENANCE_INTEGRITY",
        "scenario_id": result.run.scenario_id,
        "severity": "critical",
        "entity_refs": sorted(item["artifact"] for item in artifact_errors),
        "expected": "every artifact output_hash matches its canonical JSON payload",
        "actual": artifact_errors,
        "trace_refs": [f"{result.run.run_id}:trace"],
    }


def _is_acceptance_candidate(
    results: list[RunnerResult],
    aggregate: dict[str, Any],
    failures: list[dict[str, Any]],
    spec: dict[str, Any] | None = None,
) -> bool:
    exact_metrics = (
        "candidate_impact_precision",
        "candidate_impact_recall",
        "invalidation_precision",
        "invalidation_recall",
        "unaffected_preservation",
    )
    boolean_metrics = (
        "repair_success",
        "full_recompute_equivalent",
        "replay_determinism",
        "event_idempotency",
        "provenance_integrity",
    )
    per_scenario_pass = all(
        all(float(result.metrics[name]) == 1.0 for name in exact_metrics)
        and all(bool(result.metrics[name]) for name in boolean_metrics)
        for result in results
    )
    totals = aggregate["recompute_totals"]
    if spec is None:
        expected_recomputed = 2
        expected_total = 6
    else:
        expected_recomputed = int(spec["expected_selective_recomputed_conclusions"])
        expected_total = int(spec["expected_total_conclusions"])
    expected_ratio = expected_recomputed / expected_total
    return (
        per_scenario_pass
        and not failures
        and totals["selective_recomputed_conclusions"] == expected_recomputed
        and totals["selective_total_conclusions"] == expected_total
        and abs(totals["selective_recompute_ratio"] - expected_ratio) < 1e-12
        and totals["full_recomputed_conclusions"] == expected_total
        and totals["full_total_conclusions"] == expected_total
        and totals["full_recompute_ratio"] == 1.0
    )


def _default_paths() -> tuple[Path, Path]:
    project_root = Path(__file__).resolve().parents[3]
    return (
        project_root / "datasets" / "suites" / "p0-evolution-suite.json",
        project_root / "artifacts",
    )


def main() -> int:
    default_manifest, default_artifacts = _default_paths()
    parser = argparse.ArgumentParser(description="Run the explicit Veritas P0 evolution suite")
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument("--artifacts-root", type=Path, default=default_artifacts)
    args = parser.parse_args()

    result = run_suite(args.manifest, args.artifacts_root)
    acceptance_key = next(
        key for key in result.summary if key.endswith("acceptance_candidate")
    )
    print(
        json.dumps(
            {
                "suite_id": result.summary["suite_id"],
                "suite_version": result.summary["suite_version"],
                "summary_path": str(result.summary_path),
                "critical_failure_count": result.summary["critical_failure_count"],
                acceptance_key: result.summary[acceptance_key],
                "recompute_totals": result.summary["recompute_totals"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
