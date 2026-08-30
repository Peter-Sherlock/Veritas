"""Generate the M1-2C2 extraction benchmark (v3.0.0) and its fixtures.

v3.0.0 carries all 30 frozen v2.0.0 cases verbatim except one schema
evolution: ``canonical_key`` is removed from gold assertions and from fixture
responses. From schema ``evidence-assertion-2`` the model proposes only
``statement``/``relation``/``quote``; the deterministic layer derives the
canonical key from the statement (D-030). Identity, scoring and fixtures all
follow the derivation.

The script validates every case before writing:

- each gold quote occurs exactly once in the resolved document version;
- the gold document is retrieved within ``expected_retrieval.max_rank``;
- fixture prompt keys cover exactly the retrieved (doc, version) pairs.

Run from the repository root:

    PYTHONPATH=src python scripts/build_extraction_v3_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

from veritas.extraction.pipeline import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_prompt,
)
from veritas.providers.llm import fixture_key
from veritas.search.local_corpus import LocalCorpusProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
V2_BENCHMARK = PROJECT_ROOT / "datasets/extraction/httpx-m1-2b/benchmark.json"
V3_DIR = PROJECT_ROOT / "datasets/extraction/httpx-m1-2c"
CORPUS_ROOT = PROJECT_ROOT / "datasets/corpus/httpx-docs"

_RESPONSE_KEYS = ("statement", "relation", "quote")


def _validate_and_build() -> None:
    corpus = LocalCorpusProvider(CORPUS_ROOT)
    v2 = json.loads(V2_BENCHMARK.read_text(encoding="utf-8"))
    if len(v2["cases"]) != 30:
        raise SystemExit(f"expected 30 v2 cases, found {len(v2['cases'])}")

    benchmark = {
        "benchmark_id": "httpx-initial-extraction",
        "benchmark_version": "3.0.0",
        "corpus_id": v2["corpus_id"],
        "prompt_version": EXTRACTION_PROMPT_VERSION,
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "reasoned_at": "2026-08-30T00:00:00Z",
        "cases": [],
    }
    for case in v2["cases"]:
        benchmark["cases"].append(
            {
                "case_id": case["case_id"],
                "question": case["question"],
                "query": case["query"],
                "top_k": case["top_k"],
                **({"as_of": case["as_of"]} if case.get("as_of") else {}),
                "expected_retrieval": case["expected_retrieval"],
                "expected_assertions": [
                    {key: assertion[key] for key in ("doc_id", *_RESPONSE_KEYS)}
                    for assertion in case["expected_assertions"]
                ],
            }
        )

    report: list[str] = []
    fixture_cases: list[dict] = []
    for case in benchmark["cases"]:
        case_id = case["case_id"]
        gold_doc = case["expected_retrieval"]["doc_id"]
        max_rank = case["expected_retrieval"]["max_rank"]
        retrieved = corpus.search(case["query"], top_k=case["top_k"], as_of=case.get("as_of"))
        retrieved_ids = [result.doc_id for result in retrieved]
        rank = retrieved_ids.index(gold_doc) + 1 if gold_doc in retrieved_ids else None
        if rank is None or rank > max_rank:
            raise SystemExit(
                f"{case_id}: gold doc {gold_doc} rank {rank} exceeds max_rank {max_rank} "
                f"(retrieved: {retrieved_ids})"
            )

        versions: dict[str, str] = {}
        responses: dict[str, dict] = {}
        for result in retrieved:
            document = corpus.fetch(result.doc_id, result.version_id)
            versions[result.doc_id] = result.version_id
            gold_assertions = [
                {key: assertion[key] for key in _RESPONSE_KEYS}
                for assertion in case["expected_assertions"]
                if assertion["doc_id"] == result.doc_id
            ]
            for assertion in gold_assertions:
                occurrences = document.content.count(assertion["quote"])
                if occurrences != 1:
                    raise SystemExit(
                        f"{case_id}: quote occurs {occurrences} times in "
                        f"{result.doc_id}@{result.version_id}: {assertion['quote']!r}"
                    )
            responses[result.doc_id] = {"assertions": gold_assertions}
        expected_pairs = {
            (assertion["doc_id"], assertion["statement"]) for assertion in case["expected_assertions"]
        }
        provided_pairs = {
            (doc_id, assertion["statement"])
            for doc_id, response in responses.items()
            for assertion in response["assertions"]
        }
        if expected_pairs != provided_pairs:
            raise SystemExit(
                f"{case_id}: gold assertions are not covered by the retrieved documents: "
                f"expected {sorted(expected_pairs)}, provided {sorted(provided_pairs)}"
            )
        fixture_cases.append(
            {
                "case_id": case_id,
                "question": case["question"],
                "versions": versions,
                "responses": responses,
            }
        )
        flags = []
        if case.get("as_of"):
            flags.append(f"as_of={case['as_of']}")
        if len(case["expected_assertions"]) > 1:
            flags.append(f"multi={len(case['expected_assertions'])}")
        if any(a["relation"] == "contradicts" for a in case["expected_assertions"]):
            flags.append("contradicts")
        report.append(
            f"{case_id} gold={gold_doc}@{versions[gold_doc]} rank={rank} "
            f"assertions={len(case['expected_assertions'])} {' '.join(flags)}"
        )

    canary_case = next(case for case in benchmark["cases"] if case["case_id"] == "EX-001")
    canary_doc = next(
        case for case in fixture_cases if case["case_id"] == "EX-001"
    )["versions"]["http2"]
    canary_document = corpus.fetch("http2", canary_doc)
    canary_key = fixture_key(
        EXTRACTION_SYSTEM_PROMPT,
        build_extraction_prompt(canary_case["question"], canary_document),
    )

    fixtures = {
        "fixture_id": "httpx-extraction-fixtures-3.0.0",
        "model_id": "fixture-httpx-extractor-1",
        "prompt_canary": {
            "case_id": "EX-001",
            "doc_id": "http2",
            "version_id": canary_doc,
            "fixture_key": canary_key,
        },
        "cases": fixture_cases,
    }

    V3_DIR.mkdir(parents=True, exist_ok=True)
    benchmark_text = json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n"
    fixtures_text = json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n"
    (V3_DIR / "benchmark.json").write_bytes(benchmark_text.encode("utf-8"))
    (V3_DIR / "fixtures.json").write_bytes(fixtures_text.encode("utf-8"))

    print("\n".join(report))
    print(f"cases={len(benchmark['cases'])} canary={canary_doc}")
    print(f"wrote {V3_DIR / 'benchmark.json'}")
    print(f"wrote {V3_DIR / 'fixtures.json'}")


if __name__ == "__main__":
    _validate_and_build()
