from __future__ import annotations

from typing import Any

from veritas.domain.models import EvolutionRun


def _precision(actual: set[str], expected: set[str]) -> float:
    if not actual:
        return 1.0 if not expected else 0.0
    return len(actual & expected) / len(actual)


def _recall(actual: set[str], expected: set[str]) -> float:
    if not expected:
        return 1.0
    return len(actual & expected) / len(expected)


def evaluate_run(
    run: EvolutionRun,
    ground_truth: dict[str, Any],
    *,
    current_outcomes: dict[str, str],
    full_recompute_outcomes: dict[str, str],
    replay_determinism: bool,
    event_idempotency: bool,
    provenance_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate = set(run.candidate_impact.claims) | set(run.candidate_impact.conclusions)
    expected_candidate = set(ground_truth["reverify"])
    confirmed = {item["node_key"] for item in run.confirmed_invalidations}
    expected_confirmed = set(ground_truth["semantic_change"])
    actual_untouched = set(run.untouched_nodes)
    expected_untouched = set(ground_truth["untouched"])
    total_conclusions = int(ground_truth["total_conclusions"])

    expected_outcomes = ground_truth["expected_outcomes"]
    repair_success = all(
        current_outcomes.get(key) == expected for key, expected in expected_outcomes.items()
    )
    full_recompute_equivalent = current_outcomes == full_recompute_outcomes
    expected_recomputed = set(
        ground_truth.get(
            "expected_recomputed_conclusions",
            expected_confirmed & set(expected_outcomes),
        )
    )
    actual_recomputed = set(run.recomputed_conclusions)

    failures: list[dict[str, Any]] = []
    if candidate != expected_candidate:
        failures.append(
            _failure(
                "F01_IMPACT_DETECTION",
                run,
                candidate ^ expected_candidate,
                sorted(expected_candidate),
                sorted(candidate),
            )
        )
    if confirmed != expected_confirmed:
        failures.append(
            _failure(
                "F02_INVALIDATION_DECISION",
                run,
                confirmed ^ expected_confirmed,
                sorted(expected_confirmed),
                sorted(confirmed),
            )
        )
    if not repair_success or not full_recompute_equivalent:
        failures.append(
            _failure(
                "F03_REPAIR_CORRECTNESS",
                run,
                set(expected_outcomes),
                expected_outcomes,
                current_outcomes,
            )
        )
    if actual_untouched != expected_untouched or actual_recomputed != expected_recomputed:
        failures.append(
            _failure(
                "F04_RECOMPUTE_SCOPE",
                run,
                (actual_untouched ^ expected_untouched) | (actual_recomputed ^ expected_recomputed),
                {
                    "untouched": sorted(expected_untouched),
                    "recomputed": sorted(expected_recomputed),
                },
                {
                    "untouched": sorted(actual_untouched),
                    "recomputed": sorted(actual_recomputed),
                },
            )
        )
    if provenance_errors:
        failures.append(
            _failure(
                "F05_PROVENANCE_INTEGRITY",
                run,
                {
                    str(item.get("claim_id") or item.get("entity_id") or item.get("reason_ref"))
                    for item in provenance_errors
                },
                "all current assessments reference active evidence",
                provenance_errors,
            )
        )
    if not replay_determinism or not event_idempotency:
        failures.append(
            _failure(
                "F06_REPLAY_REPRODUCIBILITY",
                run,
                {run.change_event_id},
                {"replay_determinism": True, "event_idempotency": True},
                {
                    "replay_determinism": replay_determinism,
                    "event_idempotency": event_idempotency,
                },
            )
        )

    return {
        "candidate_impact_precision": _precision(candidate, expected_candidate),
        "candidate_impact_recall": _recall(candidate, expected_candidate),
        "invalidation_precision": _precision(confirmed, expected_confirmed),
        "invalidation_recall": _recall(confirmed, expected_confirmed),
        "unaffected_preservation": _recall(actual_untouched, expected_untouched),
        "repair_success": repair_success,
        "selective_recompute_ratio": len(run.recomputed_conclusions) / total_conclusions,
        "full_recompute_ratio": 1.0 if total_conclusions else 0.0,
        "full_recompute_equivalent": full_recompute_equivalent,
        "replay_determinism": replay_determinism,
        "event_idempotency": event_idempotency,
        "provenance_integrity": not provenance_errors,
        "provenance_errors": provenance_errors,
        "critical_failure_count": len(failures),
        "failures": failures,
        "current_outcomes": current_outcomes,
        "full_recompute_outcomes": full_recompute_outcomes,
    }


def _failure(
    failure_code: str,
    run: EvolutionRun,
    entity_refs: set[str],
    expected: Any,
    actual: Any,
) -> dict[str, Any]:
    trace_refs = [
        f"{run.run_id}:event-{int(event['event_seq']):04d}"
        for event in run.trace_events
        if entity_refs.intersection(event["entity_refs"])
    ]
    return {
        "failure_code": failure_code,
        "scenario_id": run.scenario_id,
        "severity": "critical",
        "entity_refs": sorted(entity_refs),
        "expected": expected,
        "actual": actual,
        "trace_refs": trace_refs or [f"{run.run_id}:trace"],
    }
