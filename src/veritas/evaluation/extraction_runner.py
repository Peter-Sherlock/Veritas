from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from veritas.extraction.models import ExtractionCandidateBundle, ExtractionContractError
from veritas.extraction.pipeline import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
    derive_canonical_key,
)
from veritas.extraction.store import CandidateStore, candidates_from_document
from veritas.providers.llm import (
    FixtureLLM,
    LLMProvider,
    OpenAICompatibleClient,
    RecordingLLM,
    fixture_key,
)
from veritas.search.local_corpus import LocalCorpusProvider


EXTRACTION_FAILURE_TAXONOMY = "ex-failures-1"
EX_RETRIEVAL_MISS = "EX01_RETRIEVAL_MISS"
EX_CONTRACT_REJECTION = "EX02_CONTRACT_REJECTION"
EX_CITATION_REJECTION = "EX03_CITATION_REJECTION"
EX_ASSERTION_MISMATCH = "EX04_ASSERTION_MISMATCH"
EX_FIXTURE_DRIFT = "EX05_FIXTURE_DRIFT"
EX_FAILURE_CODES = (
    EX_RETRIEVAL_MISS,
    EX_CONTRACT_REJECTION,
    EX_CITATION_REJECTION,
    EX_ASSERTION_MISMATCH,
    EX_FIXTURE_DRIFT,
)

# Contract codes whose rejection means the response never grounded in the
# document; everything else at the contract boundary is a format violation.
_CITATION_CONTRACT_CODES = {"citation_not_found", "citation_ambiguous"}


def classify_contract_error(code: str) -> tuple[str, str]:
    """Map a pipeline contract code to its EX failure code and severity."""
    if code in _CITATION_CONTRACT_CODES:
        return EX_CITATION_REJECTION, "major"
    return EX_CONTRACT_REJECTION, "critical"


def _failure_record(
    failure_code: str,
    severity: str,
    case_id: str,
    *,
    expected: str,
    actual: str,
    reason: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "failure_code": failure_code,
        "severity": severity,
        "entity_refs": [case_id],
        "expected": expected,
        "actual": actual,
    }
    if reason:
        record["reason"] = reason
    return record


def _load_json(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def build_fixture_provider(
    benchmark: dict[str, Any],
    fixtures: dict[str, Any],
    corpus: LocalCorpusProvider,
) -> FixtureLLM:
    benchmark_cases = {case["case_id"]: case for case in benchmark["cases"]}
    if len(benchmark_cases) != len(benchmark["cases"]):
        raise ValueError(f"{EX_FIXTURE_DRIFT}: duplicate case_id in benchmark")
    canary = fixtures["prompt_canary"]
    canary_case = benchmark_cases[canary["case_id"]]
    canary_document = corpus.fetch(canary["doc_id"], canary["version_id"])
    actual_canary_key = fixture_key(
        EXTRACTION_SYSTEM_PROMPT,
        build_extraction_prompt(canary_case["question"], canary_document),
    )
    if actual_canary_key != canary["fixture_key"]:
        raise ValueError(f"{EX_FIXTURE_DRIFT}: fixture prompt canary drift")

    fixture_cases = {case["case_id"]: case for case in fixtures["cases"]}
    if len(fixture_cases) != len(fixtures["cases"]):
        raise ValueError(f"{EX_FIXTURE_DRIFT}: duplicate case_id in fixture file")
    if set(fixture_cases) != set(benchmark_cases):
        raise ValueError(f"{EX_FIXTURE_DRIFT}: benchmark and fixture case sets differ")
    responses: dict[str, str] = {}
    for case in benchmark["cases"]:
        fixture_case = fixture_cases.get(case["case_id"])
        if fixture_case is None:
            raise ValueError(
                f"{EX_FIXTURE_DRIFT}: fixture missing case {case['case_id']}"
            )
        if fixture_case["question"] != case["question"]:
            raise ValueError(
                f"{EX_FIXTURE_DRIFT}: fixture question drift for {case['case_id']}"
            )
        retrieved = corpus.search(
            case["query"],
            top_k=case["top_k"],
            as_of=case.get("as_of"),
        )
        expected_docs = list(fixture_case["responses"])
        if set(fixture_case["versions"]) != set(expected_docs):
            raise ValueError(
                f"{EX_FIXTURE_DRIFT}: fixture version map differs for {case['case_id']}"
            )
        actual_docs = [result.doc_id for result in retrieved]
        if set(expected_docs) != set(actual_docs):
            raise ValueError(
                f"{EX_FIXTURE_DRIFT}: fixture retrieval snapshot drift for "
                f"{case['case_id']}: expected {expected_docs}, actual {actual_docs}"
            )
        for result in retrieved:
            document = corpus.fetch(result.doc_id, result.version_id)
            expected_version = fixture_case["versions"][result.doc_id]
            if result.version_id != expected_version:
                raise ValueError(
                    f"{EX_FIXTURE_DRIFT}: fixture version drift for "
                    f"{case['case_id']}:{result.doc_id}: "
                    f"expected {expected_version}, actual {result.version_id}"
                )
            prompt = build_extraction_prompt(case["question"], document)
            response = fixture_case["responses"][result.doc_id]
            responses[fixture_key(EXTRACTION_SYSTEM_PROMPT, prompt)] = json.dumps(
                response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
    return FixtureLLM(responses, model_id=fixtures["model_id"])


IdentityKey = tuple[str, str, str, str]


def _identity(
    doc_id: str,
    *,
    statement: str,
    relation: str,
    quote: str,
) -> tuple[IdentityKey, str]:
    """Score assertions at canonical-key level (D-030).

    The scoring identity is (doc_id, derived canonical_key, relation, quote);
    the raw statement travels alongside for human-readable failure reports,
    but case, punctuation and whitespace differences no longer break matching.
    """
    return (doc_id, derive_canonical_key(statement), relation, quote), statement


def evaluate_extraction_calibration(
    *,
    benchmark: dict[str, Any],
    fixtures: dict[str, Any],
    corpus: LocalCorpusProvider,
    provider: Any,
    on_case_done: Callable[[dict[str, Any], ExtractionCandidateBundle | None], None] | None = None,
) -> dict[str, Any]:
    if benchmark["prompt_version"] != EXTRACTION_PROMPT_VERSION:
        raise ValueError(
            f"{EX_FIXTURE_DRIFT}: benchmark prompt_version does not match runtime"
        )
    if benchmark["schema_version"] != EXTRACTION_SCHEMA_VERSION:
        raise ValueError(
            f"{EX_FIXTURE_DRIFT}: benchmark schema_version does not match runtime"
        )

    corpus_id = corpus.corpus_id
    if benchmark["corpus_id"] != corpus_id:
        raise ValueError(f"{EX_FIXTURE_DRIFT}: benchmark corpus_id does not match loaded corpus")
    pipeline = ResearchExtractionPipeline(
        corpus,
        provider,
        source_namespace=corpus_id,
    )

    case_results: list[dict[str, Any]] = []
    total_true_positive = 0
    total_expected = 0
    total_actual = 0
    reciprocal_rank_sum = 0.0
    retrieval_hits = 0
    citation_valid_cases = 0

    for case in benchmark["cases"]:
        case_id = case["case_id"]
        if not isinstance(case["top_k"], int) or case["top_k"] < 1:
            raise ValueError(f"top_k must be positive for {case_id}")
        retrieved = corpus.search(
            case["query"],
            top_k=case["top_k"],
            as_of=case.get("as_of"),
        )
        retrieved_ids = [result.doc_id for result in retrieved]
        expected_doc_id = case["expected_retrieval"]["doc_id"]
        max_rank = case["expected_retrieval"]["max_rank"]
        retrieval_rank = (
            retrieved_ids.index(expected_doc_id) + 1
            if expected_doc_id in retrieved_ids
            else None
        )
        retrieval_pass = retrieval_rank is not None and retrieval_rank <= max_rank
        if retrieval_rank is not None:
            reciprocal_rank_sum += 1.0 / retrieval_rank
        if retrieval_pass:
            retrieval_hits += 1

        expected: set[IdentityKey] = set()
        expected_statement: dict[IdentityKey, str] = {}
        for item in case["expected_assertions"]:
            identity, statement = _identity(
                item["doc_id"],
                statement=item["statement"],
                relation=item["relation"],
                quote=item["quote"],
            )
            expected.add(identity)
            expected_statement.setdefault(identity, statement)
        if len(expected) != len(case["expected_assertions"]):
            raise ValueError(f"duplicate expected assertion in {case_id}")
        case_failures: list[dict[str, Any]] = []
        if not retrieval_pass:
            case_failures.append(
                _failure_record(
                    EX_RETRIEVAL_MISS,
                    "major",
                    case_id,
                    expected=f"{expected_doc_id} retrieved within rank {max_rank}",
                    actual=(
                        f"retrieved at rank {retrieval_rank}"
                        if retrieval_rank is not None
                        else "not retrieved"
                    ),
                )
            )

        contract_rejected = False
        bundle: ExtractionCandidateBundle | None = None
        actual: set[IdentityKey] = set()
        actual_statement: dict[IdentityKey, str] = {}
        try:
            bundle = pipeline.run(
                query=case["query"],
                question=case["question"],
                reasoned_at=benchmark["reasoned_at"],
                top_k=case["top_k"],
                as_of=case.get("as_of"),
            )
            for document in bundle.documents:
                for assertion in document.assertions:
                    identity, statement = _identity(
                        document.doc_id,
                        statement=assertion.statement,
                        relation=assertion.relation,
                        quote=assertion.quote,
                    )
                    actual.add(identity)
                    actual_statement.setdefault(identity, statement)
        except ExtractionContractError as exc:
            contract_rejected = True
            failure_code, severity = classify_contract_error(exc.code)
            case_failures.append(
                _failure_record(
                    failure_code,
                    severity,
                    case_id,
                    expected="provider response passes the extraction contract",
                    actual=f"rejected ({exc.code})",
                    reason={"pipeline_code": exc.code, "message": str(exc)},
                )
            )
        if not contract_rejected:
            citation_valid_cases += 1

        true_positive = len(expected & actual)
        precision = true_positive / len(actual) if actual else float(not expected)
        recall = true_positive / len(expected) if expected else float(not actual)
        if not contract_rejected and expected != actual:
            missing = sorted(expected_statement[identity] for identity in expected - actual)
            unexpected = sorted(actual_statement[identity] for identity in actual - expected)
            case_failures.append(
                _failure_record(
                    EX_ASSERTION_MISMATCH,
                    "major",
                    case_id,
                    expected="extracted assertions exactly match gold assertions",
                    actual=f"{len(missing)} missing, {len(unexpected)} unexpected",
                    reason={
                        "missing_statements": missing,
                        "unexpected_statements": unexpected,
                    },
                )
            )

        exact_match = (not contract_rejected) and expected == actual
        case_pass = retrieval_pass and exact_match
        total_true_positive += true_positive
        total_expected += len(expected)
        total_actual += len(actual)
        case_results.append(
            {
                "case_id": case_id,
                "retrieved_doc_ids": retrieved_ids,
                "expected_doc_id": expected_doc_id,
                "retrieval_rank": retrieval_rank,
                "retrieval_pass": retrieval_pass,
                "expected_assertion_count": len(expected),
                "actual_assertion_count": len(actual),
                "assertion_precision": precision,
                "assertion_recall": recall,
                "exact_match": exact_match,
                "failures": case_failures,
                "status": "pass" if case_pass else "fail",
            }
        )
        if on_case_done is not None:
            on_case_done(case_results[-1], bundle)

    case_count = len(case_results)
    passed_case_count = sum(result["status"] == "pass" for result in case_results)
    micro_precision = (
        total_true_positive / total_actual if total_actual else float(total_expected == 0)
    )
    micro_recall = (
        total_true_positive / total_expected if total_expected else float(total_actual == 0)
    )
    all_failures = [
        record for result in case_results for record in result["failures"]
    ]
    failure_counts = {code: 0 for code in EX_FAILURE_CODES}
    for record in all_failures:
        failure_counts[record["failure_code"]] += 1
    critical_failure_count = sum(
        1 for record in all_failures if record["severity"] == "critical"
    )
    major_failure_count = sum(
        1 for record in all_failures if record["severity"] == "major"
    )
    summary = {
        "benchmark_id": benchmark["benchmark_id"],
        "benchmark_version": benchmark["benchmark_version"],
        "corpus_id": corpus_id,
        "fixture_id": fixtures["fixture_id"],
        "model_id": fixtures["model_id"],
        "prompt_version": EXTRACTION_PROMPT_VERSION,
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "failure_taxonomy": EXTRACTION_FAILURE_TAXONOMY,
        "case_count": case_count,
        "passed_case_count": passed_case_count,
        "critical_failure_count": critical_failure_count,
        "major_failure_count": major_failure_count,
        "failure_counts": failure_counts,
        "failures": all_failures,
        "metrics": {
            "retrieval_hit_at_k": retrieval_hits / case_count if case_count else 0.0,
            "mean_reciprocal_rank": reciprocal_rank_sum / case_count if case_count else 0.0,
            "assertion_micro_precision": micro_precision,
            "assertion_micro_recall": micro_recall,
            "citation_exact_alignment": (
                citation_valid_cases / case_count if case_count else 0.0
            ),
        },
        "m1_2a_acceptance_candidate": critical_failure_count == 0
        and major_failure_count == 0,
        "cases": case_results,
    }
    canonical = json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    summary["content_hash"] = hashlib.sha256(canonical).hexdigest()
    return summary


def _persist_case_candidates(
    store: CandidateStore,
    bundle: ExtractionCandidateBundle,
    *,
    source_namespace: str,
    run_id: str,
    observed_at: str,
) -> dict[str, int]:
    records = [
        record
        for document in bundle.documents
        for record in candidates_from_document(document, source_namespace=source_namespace)
    ]
    return store.persist(records, run_id=run_id, observed_at=observed_at)


def run_extraction_calibration(
    *,
    benchmark_path: str | Path,
    fixtures_path: str | Path,
    corpus_root: str | Path,
    store_path: str | Path | None = None,
) -> dict[str, Any]:
    benchmark = _load_json(benchmark_path)
    fixtures = _load_json(fixtures_path)
    corpus = LocalCorpusProvider(corpus_root)
    provider = build_fixture_provider(benchmark, fixtures, corpus)
    store = CandidateStore(store_path) if store_path else None
    try:
        on_case_done = None
        if store is not None:
            run_id = f"fixture:{fixtures['fixture_id']}"

            def on_case_done(
                case_result: dict[str, Any],
                bundle: ExtractionCandidateBundle | None,
            ) -> None:
                if bundle is not None:
                    _persist_case_candidates(
                        store,
                        bundle,
                        source_namespace=corpus.corpus_id,
                        run_id=run_id,
                        observed_at=benchmark["reasoned_at"],
                    )

        summary = evaluate_extraction_calibration(
            benchmark=benchmark,
            fixtures=fixtures,
            corpus=corpus,
            provider=provider,
            on_case_done=on_case_done,
        )
        if store is not None:
            counts = store.counts()
            print(
                "candidate store: "
                f"candidates={counts['candidates']} "
                f"observations={counts['observations']} "
                f"distinct_canonical_keys={counts['distinct_canonical_keys']}",
                file=sys.stderr,
            )
        return summary
    finally:
        if store is not None:
            store.close()


def run_live_extraction_calibration(
    *,
    benchmark_path: str | Path,
    corpus_root: str | Path,
    model: str,
    base_url: str,
    record_path: str | Path,
    provider: LLMProvider | None = None,
    store_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the frozen benchmark against a live provider and record responses.

    Benchmark, corpus, prompts and metrics are identical to the fixture path;
    only the provider changes. Every exchange is recorded under
    ``record_path`` so the run can later be replayed deterministically. The
    recording file is rewritten after every case, so an interrupted run keeps
    all completed exchanges, and per-case progress is streamed to stderr.
    When ``store_path`` is given, each case's contract-valid candidates are
    committed in their own transaction as the case completes, so an
    interrupted run keeps every persisted case and a replayed run is
    idempotent against the same store.
    """
    benchmark = _load_json(benchmark_path)
    corpus = LocalCorpusProvider(corpus_root)
    if provider is None:
        # DeepSeek V4-Flash defaults to thinking mode; calibration pins
        # non-thinking for latency, cost and near-deterministic JSON output.
        provider = OpenAICompatibleClient(
            model=model,
            base_url=base_url,
            extra_payload={"thinking": {"type": "disabled"}},
        )
    recorder = RecordingLLM(provider)
    store = CandidateStore(store_path) if store_path else None
    try:
        store_run_id = f"live:{model}"
        record_output = Path(record_path)
        record_output.parent.mkdir(parents=True, exist_ok=True)
        case_total = len(benchmark["cases"])
        case_done = 0

        def on_case_done(
            case_result: dict[str, Any],
            bundle: ExtractionCandidateBundle | None,
        ) -> None:
            nonlocal case_done
            case_done += 1
            if store is not None and bundle is not None:
                _persist_case_candidates(
                    store,
                    bundle,
                    source_namespace=corpus.corpus_id,
                    run_id=store_run_id,
                    observed_at=benchmark["reasoned_at"],
                )
            recorder.save(record_output)
            print(
                f"[live] {case_done}/{case_total} {case_result['case_id']} "
                f"{case_result['status']} requests={recorder.request_count} "
                f"prompt_tokens={recorder.prompt_tokens}",
                file=sys.stderr,
                flush=True,
            )

        summary = evaluate_extraction_calibration(
            benchmark=benchmark,
            fixtures={"fixture_id": f"live-recording:{model}", "model_id": model},
            corpus=corpus,
            provider=recorder,
            on_case_done=on_case_done,
        )
        recorder.save(record_output)
        print(
            f"live provider recording: model={model} "
            f"requests={recorder.request_count} "
            f"prompt_tokens={recorder.prompt_tokens} "
            f"completion_tokens={recorder.completion_tokens}",
            file=sys.stderr,
        )
        if store is not None:
            counts = store.counts()
            print(
                "candidate store: "
                f"candidates={counts['candidates']} "
                f"observations={counts['observations']} "
                f"distinct_canonical_keys={counts['distinct_canonical_keys']}",
                file=sys.stderr,
            )
        return summary
    finally:
        if store is not None:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the frozen extraction calibration")
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument(
        "--provider",
        choices=("fixture", "live"),
        default="fixture",
        help="fixture replays frozen responses; live calls an OpenAI-compatible API",
    )
    parser.add_argument("--fixtures", help="required with --provider fixture")
    parser.add_argument(
        "--model",
        default="deepseek-v4-flash",
        help="live provider model id",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.deepseek.com",
        help="live provider OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--record-out",
        help="required with --provider live; path for the recorded responses",
    )
    parser.add_argument(
        "--store-out",
        help="optional SQLite path; contract-valid candidates are persisted "
        "transactionally per case with content-hash dedup",
    )
    parser.add_argument("--output")
    parser.add_argument("--assert-pass", action="store_true")
    args = parser.parse_args()

    if args.provider == "fixture":
        if not args.fixtures:
            parser.error("--provider fixture requires --fixtures")
        summary = run_extraction_calibration(
            benchmark_path=args.benchmark,
            fixtures_path=args.fixtures,
            corpus_root=args.corpus_root,
            store_path=args.store_out,
        )
    else:
        if not args.record_out:
            parser.error("--provider live requires --record-out")
        try:
            summary = run_live_extraction_calibration(
                benchmark_path=args.benchmark,
                corpus_root=args.corpus_root,
                model=args.model,
                base_url=args.base_url,
                record_path=args.record_out,
                store_path=args.store_out,
            )
        except ValueError as exc:
            parser.error(str(exc))
    serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(serialized.encode("utf-8"))
    print(serialized, end="")
    if args.assert_pass and not summary["m1_2a_acceptance_candidate"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
