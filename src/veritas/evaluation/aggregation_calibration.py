"""Frozen aggregation calibration: deterministic clusterer vs live recording (M2-1/M2-2, D-040).

Replays the committed DeepSeek recording through the extraction pipeline,
pairs the 52 resulting candidate statements with the 32 gold assertions of
the 30-question benchmark, and measures how many gold assertions a
deterministic paraphrase cluster recovers compared with exact canonical
keys. The policy threshold (0.375) was calibrated on this frozen pairing
and is itself part of the pinned contract: re-running this module always
measures the same contract, never re-tunes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from veritas.aggregation.clusterer import ClusterPolicy, similarity
from veritas.evaluation.scenario import canonical_json
from veritas.extraction.pipeline import (
    ResearchExtractionPipeline,
    derive_canonical_key,
)
from veritas.providers.llm import FixtureLLM
from veritas.search.local_corpus import LocalCorpusProvider


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def live_candidates(
    benchmark: dict[str, Any], recording: dict[str, Any], corpus: LocalCorpusProvider
) -> dict[tuple[str, str], str]:
    """(doc_id, canonical_key) -> statement for every replayed live candidate."""
    pipeline = ResearchExtractionPipeline(
        corpus,
        FixtureLLM(recording["responses"], model_id=recording["model_id"]),
        source_namespace=corpus.corpus_id,
    )
    candidates: dict[tuple[str, str], str] = {}
    for case in benchmark["cases"]:
        try:
            bundle = pipeline.run(
                query=case["query"],
                question=case["question"],
                reasoned_at=benchmark["reasoned_at"],
                top_k=case["top_k"],
                as_of=case.get("as_of"),
            )
        except Exception:
            # Contract-rejected cases contribute no candidates; their
            # distribution is pinned by the live replay tests.
            continue
        for document in bundle.documents:
            for assertion in document.assertions:
                candidates.setdefault(
                    (document.doc_id, derive_canonical_key(assertion.statement)),
                    assertion.statement,
                )
    return candidates


def gold_assertions(benchmark: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"case_id": case["case_id"], "doc_id": item["doc_id"], "statement": item["statement"]}
        for case in benchmark["cases"]
        for item in case["expected_assertions"]
    ]


def run_calibration(
    *,
    benchmark_path: str | Path,
    recording_path: str | Path,
    corpus_root: str | Path,
    policy: ClusterPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or ClusterPolicy()
    benchmark = json.loads(Path(benchmark_path).read_text(encoding="utf-8"))
    recording = json.loads(Path(recording_path).read_text(encoding="utf-8"))
    corpus = LocalCorpusProvider(corpus_root)
    candidates = live_candidates(benchmark, recording, corpus)
    gold = gold_assertions(benchmark)

    matched_cases: list[dict[str, Any]] = []
    exact_covered: list[str] = []
    cluster_covered: list[str] = []
    for assertion in gold:
        case_id = assertion["case_id"]
        doc_id = assertion["doc_id"]
        statement = assertion["statement"]
        best_score: float | None = None
        best_statement: str | None = None
        for (doc, key), candidate in candidates.items():
            if doc != doc_id:
                continue
            if (doc, key) == (doc_id, derive_canonical_key(statement)):
                if case_id not in exact_covered:
                    exact_covered.append(case_id)
            score = similarity(statement, candidate)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best_score, best_statement = score, candidate
        if best_score is not None and best_score >= policy.min_jaccard:
            cluster_covered.append(case_id)
            matched_cases.append(
                {
                    "case_id": case_id,
                    "doc_id": doc_id,
                    "score": best_score,
                    "gold_statement": statement,
                    "live_statement": best_statement,
                }
            )

    matched_cases.sort(key=lambda item: item["case_id"])
    exact_covered.sort()
    cluster_covered.sort()
    return {
        "calibration_id": "m2-1-aggregation-calibration",
        "calibration_version": "1.0.0",
        "recording_model": recording["model_id"],
        "policy": {
            "rule_version": policy.rule_version,
            "min_jaccard": policy.min_jaccard,
        },
        "counts": {
            "gold_assertions": len(gold),
            "live_candidates": len(candidates),
            "exact_key_covered": len(exact_covered),
            "cluster_covered": len(cluster_covered),
        },
        "exact_key_covered_cases": exact_covered,
        "cluster_covered_cases": cluster_covered,
        "matched_pairs": matched_cases,
    }


def write_summary(summary: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(summary)
    output_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    path.write_text(
        json.dumps(
            {**payload, "output_hash": output_hash},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    root = _project_root()
    parser = argparse.ArgumentParser(
        description="Run the frozen M2-1 aggregation calibration against the live recording"
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=root / "datasets" / "extraction" / "httpx-m1-2c" / "benchmark.json",
    )
    parser.add_argument(
        "--recording",
        type=Path,
        default=root
        / "artifacts"
        / "extraction"
        / "httpx-initial-extraction-3.0.0-deepseek-v4-flash"
        / "responses-recording.json",
    )
    parser.add_argument(
        "--corpus-root", type=Path, default=root / "datasets" / "corpus" / "httpx-docs"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "artifacts" / "aggregation" / "m2-1-calibration" / "summary.json",
    )
    args = parser.parse_args()

    summary = run_calibration(
        benchmark_path=args.benchmark,
        recording_path=args.recording,
        corpus_root=args.corpus_root,
    )
    path = write_summary(summary, args.output)
    print(
        json.dumps(
            {
                "calibration_id": summary["calibration_id"],
                "exact_key_covered": summary["counts"]["exact_key_covered"],
                "cluster_covered": summary["counts"]["cluster_covered"],
                "gold_assertions": summary["counts"]["gold_assertions"],
                "min_jaccard": summary["policy"]["min_jaccard"],
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
