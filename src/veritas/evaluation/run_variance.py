"""Run-to-run variance between two live extractions under one contract (M2-3, D-045).

Gate M1 carried item C3-R: model-capability conclusions need a second run
under the SAME contract, because a single live run's failure distribution
is not a capability verdict. This module compares two committed live
recordings (replayed deterministically through the frozen pipeline) at
three levels:

* **run level** — candidate count, distinct canonical keys, contract
  rejections;
* **key level** — the overlap of ``(doc_id, canonical_key)`` identity
  sets across runs (the honest repeat-rate of the model's assertions);
* **case level** — per-benchmark-case agreement of the asserted sets.

The comparison is fully deterministic given the two frozen recordings;
the summary is canonical JSON with a pinned ``output_hash``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from veritas.evaluation.aggregation_calibration import write_summary
from veritas.extraction.models import ExtractionContractError
from veritas.extraction.pipeline import (
    ResearchExtractionPipeline,
    derive_canonical_key,
)
from veritas.providers.llm import FixtureLLM
from veritas.search.local_corpus import LocalCorpusProvider


def _load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _replay_run(
    benchmark: dict[str, Any],
    recording: dict[str, Any],
    corpus: LocalCorpusProvider,
) -> tuple[dict[tuple[str, str], str], list[str]]:
    """Replay one committed recording; return candidates and rejected cases."""
    pipeline = ResearchExtractionPipeline(
        corpus,
        FixtureLLM(recording["responses"], model_id=recording["model_id"]),
        source_namespace=corpus.corpus_id,
    )
    candidates: dict[tuple[str, str], str] = {}
    rejected: list[str] = []
    for case in benchmark["cases"]:
        try:
            bundle = pipeline.run(
                query=case["query"],
                question=case["question"],
                reasoned_at=benchmark["reasoned_at"],
                top_k=case["top_k"],
                as_of=case.get("as_of"),
            )
        except ExtractionContractError:
            rejected.append(case["case_id"])
            continue
        for document in bundle.documents:
            for assertion in document.assertions:
                candidates.setdefault(
                    (document.doc_id, derive_canonical_key(assertion.statement)),
                    assertion.statement,
                )
    return candidates, sorted(rejected)


def run_variance(
    *,
    benchmark_path: str | Path,
    recording_a_path: str | Path,
    recording_b_path: str | Path,
    corpus_root: str | Path,
    label_a: str = "run1",
    label_b: str = "run2",
) -> dict[str, Any]:
    benchmark = _load(benchmark_path)
    recording_a = _load(recording_a_path)
    recording_b = _load(recording_b_path)
    corpus = LocalCorpusProvider(corpus_root)
    candidates_a, rejected_a = _replay_run(benchmark, recording_a, corpus)
    candidates_b, rejected_b = _replay_run(benchmark, recording_b, corpus)

    keys_a = set(candidates_a)
    keys_b = set(candidates_b)
    shared = keys_a & keys_b

    def per_run_profile(candidates: dict[tuple[str, str], str], rejected: list[str]) -> dict[str, Any]:
        return {
            "candidates": len(candidates),
            "distinct_keys": len(set(candidates)),
            "rejected_cases": len(rejected),
        }

    def case_assertions(candidates: dict[tuple[str, str], str]) -> dict[str, set[tuple[str, str]]]:
        per_case: dict[str, set[tuple[str, str]]] = {}
        for case in benchmark["cases"]:
            doc_id = case["expected_retrieval"]["doc_id"]
            per_case[case["case_id"]] = {
                key for (doc, key) in candidates if doc == doc_id
            }
        return per_case

    cases_a = case_assertions(candidates_a)
    cases_b = case_assertions(candidates_b)
    case_buckets: dict[str, list[str]] = {
        "identical": [],
        "a_only": [],
        "b_only": [],
        "both_differ": [],
    }
    for case_id in sorted(cases_a):
        set_a, set_b = cases_a[case_id], cases_b[case_id]
        if not set_a and not set_b:
            case_buckets["identical"].append(case_id)  # both rejected/empty
        elif set_a and set_a == set_b:
            case_buckets["identical"].append(case_id)
        elif set_a and not set_b:
            case_buckets["a_only"].append(case_id)
        elif set_b and not set_a:
            case_buckets["b_only"].append(case_id)
        else:
            case_buckets["both_differ"].append(case_id)

    union = keys_a | keys_b
    return {
        "variance_id": "m2-3-run-variance",
        "variance_version": "1.0.0",
        "benchmark_id": benchmark["benchmark_id"],
        "benchmark_version": benchmark["benchmark_version"],
        "contract": {
            "prompt_version": benchmark["prompt_version"],
            "schema_version": benchmark["schema_version"],
        },
        "models": {
            label_a: recording_a["model_id"],
            label_b: recording_b["model_id"],
        },
        "runs": {
            label_a: per_run_profile(candidates_a, rejected_a),
            label_b: per_run_profile(candidates_b, rejected_b),
        },
        "rejected_cases": {
            label_a: rejected_a,
            label_b: rejected_b,
            "shared_rejections": sorted(set(rejected_a) & set(rejected_b)),
        },
        "key_level": {
            "shared_keys": len(shared),
            "only_a": len(keys_a - keys_b),
            "only_b": len(keys_b - keys_a),
            "jaccard": (len(shared) / len(union)) if union else 1.0,
        },
        "case_level": {
            name: len(buckets) for name, buckets in case_buckets.items()
        },
        "case_buckets": {name: sorted(buckets) for name, buckets in case_buckets.items()},
    }


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Compare two committed live recordings under one extraction contract"
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=root / "datasets" / "extraction" / "httpx-m1-2c" / "benchmark.json",
    )
    parser.add_argument("--recording-a", type=Path, required=True)
    parser.add_argument("--recording-b", type=Path, required=True)
    parser.add_argument(
        "--corpus-root", type=Path, default=root / "datasets" / "corpus" / "httpx-docs"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "artifacts" / "extraction" / "m2-3-run-variance" / "summary.json",
    )
    args = parser.parse_args()

    summary = run_variance(
        benchmark_path=args.benchmark,
        recording_a_path=args.recording_a,
        recording_b_path=args.recording_b,
        corpus_root=args.corpus_root,
    )
    path = write_summary(summary, args.output)
    print(
        json.dumps(
            {
                "variance_id": summary["variance_id"],
                "key_level": summary["key_level"],
                "case_level": summary["case_level"],
                "summary_path": str(path),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
