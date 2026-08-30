"""Generate the expanded M1-2B2 extraction benchmark (v2.0.0) and its fixtures.

v2.0.0 carries the frozen v1.0.0 cases verbatim and appends 20 new cases
(EX-011..EX-030), including multi-assertion cases, contradicts relations, and
two as_of version-view cases. Fixture responses are the deterministic
perfect-model stand-in: the gold document returns the gold assertion set, all
other retrieved documents return an empty assertions list.

The script validates every case before writing:

- each gold quote occurs exactly once in the resolved document version;
- the gold document is retrieved within ``expected_retrieval.max_rank``;
- fixture prompt keys cover exactly the retrieved (doc, version) pairs.

Run from the repository root:

    PYTHONPATH=src python scripts/build_extraction_v2_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_prompt,
)
from veritas.providers.llm import fixture_key
from veritas.search.local_corpus import LocalCorpusProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
V1_BENCHMARK = (
    PROJECT_ROOT / "datasets/extraction/httpx-m1-2a/benchmark.json"
)
V2_DIR = PROJECT_ROOT / "datasets/extraction/httpx-m1-2b"
CORPUS_ROOT = PROJECT_ROOT / "datasets/corpus/httpx-docs"

NEW_CASES: list[dict] = [
    {
        "case_id": "EX-011",
        "question": "What is HTTPX's default timeout for network inactivity?",
        "query": "default timeout network inactivity five seconds",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "quickstart", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "quickstart",
                "statement": "HTTPX default timeout for network inactivity is five seconds",
                "canonical_key": "httpx.timeout.default=5s",
                "relation": "supports",
                "quote": (
                    "The default timeout for network inactivity is five seconds. "
                    "You can modify the\nvalue to be more or less strict:"
                ),
            }
        ],
    },
    {
        "case_id": "EX-012",
        "question": "How are complicated JSON request bodies sent with HTTPX?",
        "query": "sending json encoded data request body",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "quickstart", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "quickstart",
                "statement": "Complicated request data structures are sent using JSON encoding instead of form encoding",
                "canonical_key": "httpx.request.json_parameter",
                "relation": "supports",
                "quote": (
                    "Form encoded data is okay if all you need is a simple key-value data structure.\n"
                    "For more complicated data structures you'll often want to use JSON encoding instead."
                ),
            }
        ],
    },
    {
        "case_id": "EX-013",
        "question": "How does HTTPX handle line endings when streaming text line by line?",
        "query": "universal line endings iter_lines streaming",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "quickstart", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "quickstart",
                "statement": "HTTPX stream iteration normalises all line endings to \\n",
                "canonical_key": "httpx.stream.iter_lines.universal_endings",
                "relation": "supports",
                "quote": "HTTPX will use universal line endings, normalising all cases to `\\n`.",
            }
        ],
    },
    {
        "case_id": "EX-014",
        "question": "What does HTTPX document about streaming large responses?",
        "query": "streaming responses large downloads memory",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "quickstart", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "quickstart",
                "statement": "Streaming responses are consumed inside a with block without loading the whole body into memory",
                "canonical_key": "httpx.stream.context_manager",
                "relation": "supports",
                "quote": (
                    "For large downloads you may want to use streaming responses that do not "
                    "load the entire response body into memory at once."
                ),
            },
            {
                "doc_id": "quickstart",
                "statement": "response.content and response.text are unavailable inside a streaming block",
                "canonical_key": "httpx.stream.unavailable_attributes",
                "relation": "supports",
                "quote": (
                    "If you're using streaming responses in any of these ways then the "
                    "`response.content` and `response.text` attributes will not be available, "
                    "and will raise errors if accessed."
                ),
            },
        ],
    },
    {
        "case_id": "EX-015",
        "question": "Which async environments does HTTPX support?",
        "query": "supported async environments trio asyncio backend",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "async", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "async",
                "statement": "HTTPX supports asyncio and trio as async environments",
                "canonical_key": "httpx.async.backends",
                "relation": "supports",
                "quote": "HTTPX supports either `asyncio` or `trio` as an async environment.",
            }
        ],
    },
    {
        "case_id": "EX-016",
        "question": "How do you close an AsyncClient explicitly?",
        "query": "close async client explicitly aclose",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "async", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "async",
                "statement": "An AsyncClient can be closed explicitly with await client.aclose()",
                "canonical_key": "httpx.async_client.aclose",
                "relation": "supports",
                "quote": (
                    "Alternatively, use `await client.aclose()` if you want to close a "
                    "client explicitly:"
                ),
            }
        ],
    },
    {
        "case_id": "EX-017",
        "question": "Does HTTPX follow redirects by default like requests?",
        "query": "redirects follow default requests unlike",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "compatibility", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "compatibility",
                "statement": "HTTPX does not follow redirects by default, unlike requests",
                "canonical_key": "httpx.redirects.follow_default=false",
                "relation": "contradicts",
                "quote": "Unlike `requests`, HTTPX does **not follow redirects by default**.",
            }
        ],
    },
    {
        "case_id": "EX-018",
        "question": "Does HTTPX encode string request bodies as latin1 like Requests?",
        "query": "utf-8 encoding request bodies latin1",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "compatibility", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "compatibility",
                "statement": "HTTPX encodes str request bodies as utf-8, not latin1 like Requests",
                "canonical_key": "httpx.request.body_encoding=utf-8",
                "relation": "contradicts",
                "quote": "HTTPX uses `utf-8` for encoding `str` request bodies.",
            }
        ],
    },
    {
        "case_id": "EX-019",
        "question": "Can cookies be passed per request on a Client instance?",
        "query": "cookies client instance set rather than per request",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "compatibility", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "compatibility",
                "statement": "Cookies must be set on the client instance, not passed per request",
                "canonical_key": "httpx.client.cookies=client_instance_only",
                "relation": "contradicts",
                "quote": (
                    "This usage is **not** supported:\n\n```python\n"
                    "client = httpx.Client()\nclient.post(..., cookies=...)\n```"
                ),
            }
        ],
    },
    {
        "case_id": "EX-020",
        "question": "What does the NO_PROXY environment variable do in HTTPX?",
        "query": "NO_PROXY disables proxy specific urls",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "environment_variables", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "environment_variables",
                "statement": "NO_PROXY disables the proxy for a comma-separated list of hostnames and urls",
                "canonical_key": "httpx.env.no_proxy",
                "relation": "supports",
                "quote": "`NO_PROXY` disables the proxy for specific urls",
            }
        ],
    },
    {
        "case_id": "EX-021",
        "question": "How do you make HTTPX ignore environment variables?",
        "query": "trust_env ignore environment variables false",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "environment_variables", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "environment_variables",
                "statement": "Environment variables are ignored when trust_env is set to False",
                "canonical_key": "httpx.client.trust_env=false",
                "relation": "supports",
                "quote": (
                    "Environment variables are used by default. To ignore environment "
                    "variables, `trust_env` has to be set `False`."
                ),
            }
        ],
    },
    {
        "case_id": "EX-022",
        "question": "Does HTTPX properly support HTTPS proxies?",
        "query": "HTTPS proxy properly support error",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "troubleshooting", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "troubleshooting",
                "statement": "HTTPX does not properly support HTTPS proxies at this time",
                "canonical_key": "httpx.proxy.https_support=false",
                "relation": "contradicts",
                "quote": "HTTPX does not properly support HTTPS proxies at this time.",
            }
        ],
    },
    {
        "case_id": "EX-023",
        "question": "How is the proxy handshake timeout on HTTPS requests resolved?",
        "query": "handshake operation timed out scheme change proxy",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "troubleshooting", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "troubleshooting",
                "statement": "HTTPS proxy handshake timeouts are fixed by changing the proxy scheme from https:// to http://",
                "canonical_key": "httpx.proxy.scheme_fix=http",
                "relation": "supports",
                "quote": (
                    "Change the scheme of your HTTPS proxy to `http://...` instead of "
                    "`https://...`:"
                ),
            }
        ],
    },
    {
        "case_id": "EX-024",
        "question": "What event hooks does the HTTPX client support?",
        "query": "event hooks request response called",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "advanced", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "advanced",
                "statement": "Client event hooks are called every time a particular type of event takes place",
                "canonical_key": "httpx.client.event_hooks",
                "relation": "supports",
                "quote": (
                    'HTTPX allows you to register "event hooks" with the client, that are called\n'
                    "every time a particular type of event takes place."
                ),
            },
            {
                "doc_id": "advanced",
                "statement": "HTTPX currently has two event hooks: request and response",
                "canonical_key": "httpx.client.event_hooks.types",
                "relation": "supports",
                "quote": "There are currently two event hooks:",
            },
        ],
    },
    {
        "case_id": "EX-025",
        "question": "How is the HTTPX connection pool size configured?",
        "query": "connection pool limits max_connections keepalive",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "advanced", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "advanced",
                "statement": "Connection pool size is controlled with the limits keyword argument on the client",
                "canonical_key": "httpx.limits.argument",
                "relation": "supports",
                "quote": (
                    "You can control the connection pool size using the `limits` keyword\n"
                    "argument on the client."
                ),
            }
        ],
    },
    {
        "case_id": "EX-026",
        "question": "How can HTTPX transports be mocked during testing?",
        "query": "mock transport handler testing responses",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "advanced", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "advanced",
                "statement": "MockTransport maps requests onto pre-determined responses during testing",
                "canonical_key": "httpx.transport.mock_transport",
                "relation": "supports",
                "quote": (
                    "The `httpx.MockTransport` class accepts a handler function, which can be used\n"
                    "to map requests onto pre-determined responses:"
                ),
            }
        ],
    },
    {
        "case_id": "EX-027",
        "question": "How is a custom CA bundle supplied for SSL verification?",
        "query": "custom CA bundle verify parameter ssl",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "advanced", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "advanced",
                "statement": "A custom CA bundle is passed via the verify parameter",
                "canonical_key": "httpx.ssl.verify=custom_ca_bundle",
                "relation": "supports",
                "quote": "If you'd like to use a custom CA bundle, you can use the `verify` parameter.",
            }
        ],
    },
    {
        "case_id": "EX-028",
        "question": "What does the Response.elapsed property measure?",
        "query": "elapsed timedelta amount of time",
        "top_k": 3,
        "expected_retrieval": {"doc_id": "api", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "api",
                "statement": "Response.elapsed measures the time between sending the request and closing the response",
                "canonical_key": "httpx.response.elapsed",
                "relation": "supports",
                "quote": (
                    "The amount of time elapsed between sending the request and calling "
                    "`close()` on the corresponding response received for that request."
                ),
            }
        ],
    },
    {
        "case_id": "EX-029",
        "question": "Which Python version did the HTTPX 0.24.1 documentation require?",
        "query": "requires python installation pip",
        "top_k": 3,
        "as_of": "2023-06-01T00:00:00Z",
        "expected_retrieval": {"doc_id": "index", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "index",
                "statement": "HTTPX 0.24.1 requires Python 3.7 or later",
                "canonical_key": "httpx.install.python_minimum=3.7",
                "relation": "supports",
                "quote": "HTTPX requires Python 3.7+",
            }
        ],
    },
    {
        "case_id": "EX-030",
        "question": "How was a proxy configured on httpx.Client before the 0.26 transport mounts style?",
        "query": "setup proxies telling connect",
        "top_k": 3,
        "as_of": "2023-12-01T00:00:00Z",
        "expected_retrieval": {"doc_id": "troubleshooting", "max_rank": 3},
        "expected_assertions": [
            {
                "doc_id": "troubleshooting",
                "statement": "Before 0.26, proxies were configured with a proxies dict mapping schemes to proxy urls",
                "canonical_key": "httpx.client.proxies_dict=legacy",
                "relation": "supports",
                "quote": (
                    'proxies = {\n  "http://": "http://myproxy.org",\n'
                    '  "https://": "https://myproxy.org",\n}'
                ),
            }
        ],
    },
]


def _validate_and_build() -> None:
    corpus = LocalCorpusProvider(CORPUS_ROOT)
    v1 = json.loads(V1_BENCHMARK.read_text(encoding="utf-8"))

    benchmark = {
        "benchmark_id": "httpx-initial-extraction",
        "benchmark_version": "2.0.0",
        "corpus_id": v1["corpus_id"],
        "prompt_version": v1["prompt_version"],
        "schema_version": v1["schema_version"],
        "reasoned_at": "2026-08-30T00:00:00Z",
        "cases": [dict(case) for case in v1["cases"]],
    }
    seen_ids = {case["case_id"] for case in benchmark["cases"]}
    for case in NEW_CASES:
        if case["case_id"] in seen_ids:
            raise SystemExit(f"duplicate case id {case['case_id']}")
        seen_ids.add(case["case_id"])
        benchmark["cases"].append(dict(case))

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
                {key: assertion[key] for key in ("statement", "canonical_key", "relation", "quote")}
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
        "fixture_id": "httpx-extraction-fixtures-2.0.0",
        "model_id": "fixture-httpx-extractor-1",
        "prompt_canary": {
            "case_id": "EX-001",
            "doc_id": "http2",
            "version_id": canary_doc,
            "fixture_key": canary_key,
        },
        "cases": fixture_cases,
    }

    V2_DIR.mkdir(parents=True, exist_ok=True)
    benchmark_text = json.dumps(benchmark, ensure_ascii=False, indent=2) + "\n"
    fixtures_text = json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n"
    (V2_DIR / "benchmark.json").write_bytes(benchmark_text.encode("utf-8"))
    (V2_DIR / "fixtures.json").write_bytes(fixtures_text.encode("utf-8"))

    print("\n".join(report))
    print(f"cases={len(benchmark['cases'])} canary={canary_doc}")
    print(f"wrote {V2_DIR / 'benchmark.json'}")
    print(f"wrote {V2_DIR / 'fixtures.json'}")


if __name__ == "__main__":
    _validate_and_build()
