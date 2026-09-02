"""Scaled evolution benchmark over real corpus history (M1-5B, D-038).

Gate P0 condition 2 (D-021) forbids extrapolating cost claims from the
frozen 4-of-11 P0 scenario ratio. This benchmark grounds the claim in the
real HTTPX corpus history instead:

* a T0 research graph is built from six documents via the deterministic
  extraction pipeline, each contributing one watched claim, six per-claim
  conclusions and one cross-document aggregate conclusion;
* a timeline of thirteen real content revisions (every ``published_at``
  comes from the corpus manifest; version steps that share a content hash
  are skipped) is applied through :class:`EvolutionEngine`; nine of them
  leave the watched fact intact (evidence re-attaches to the same claim
  from the new version) and four remove the watched sentence, so the claim
  flips and re-research produces a replacement claim;
* after every event the stored state is compared against a full recompute
  of every claim and conclusion in the repository — the P0
  ``full_recompute_equivalent`` metric, applied per event on a real graph;
* cost accounting compares the engine's actual selective work (claim
  re-assessments + conclusion recomputations) against the counterfactual
  size of a full recompute (all claims + all conclusions) at the same
  point in the timeline.

The summary is canonical JSON with a pinned ``output_hash`` so the frozen
artifact can be byte-compared in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veritas.evidence.rules import evaluate_claim, evaluate_conclusion
from veritas.evaluation.scenario import canonical_json
from veritas.extraction.models import ExtractionCandidateBundle
from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
    derive_canonical_key,
)
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.integration import GraphBridge
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository

BENCHMARK_ID = "m1-5b-evolution-benchmark"
BENCHMARK_VERSION = "1.0.0"
SCENARIO_ID = "M1-5B"
SCENARIO_VERSION = "1.0.0"
PROJECT_ID = "httpx-research"
RULE_VERSION = "p0-rules-2"
T0_AS_OF = "2023-06-01T00:00:00Z"
T0_REASONED_AT = "2026-08-30T00:00:00Z"
TOP_K = 3


class BenchmarkError(ValueError):
    """A stable, classifiable failure of the evolution benchmark."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class WatchedAssertion:
    """The assertion the fixture model states for a document version."""

    version_id: str
    quote: str
    statement: str


@dataclass(frozen=True)
class BenchmarkDoc:
    doc_id: str
    question: str
    query: str
    watched: tuple[WatchedAssertion, ...]

    def watched_for(self, version_id: str) -> WatchedAssertion:
        for assertion in self.watched:
            if assertion.version_id == version_id:
                return assertion
        raise BenchmarkError(
            "unplanned_extraction_era",
            f"{self.doc_id!r} was retrieved at {version_id!r}, which the "
            "benchmark plan does not cover; extend the plan or the era is wrong",
        )


@dataclass(frozen=True)
class BenchmarkRevision:
    doc_id: str
    old_version_id: str
    new_version_id: str


# Real sentences from the pinned corpus snapshots; each occurs exactly once
# in every version where it is asserted (validated by validate_plan).
BENCHMARK_DOCS: tuple[BenchmarkDoc, ...] = (
    BenchmarkDoc(
        doc_id="advanced",
        question="When should I use a Client instance instead of the top-level API?",
        query="client instance connection pooling experimentation",
        watched=(
            WatchedAssertion(
                "0.24.1",
                "If you do anything more than experimentation, one-off scripts, or "
                "prototypes, then you should use a `Client` instance.",
                "HTTPX clients should be used for anything more than experimentation, "
                "one-off scripts, or prototypes.",
            ),
            WatchedAssertion(
                "0.25.2",
                "If you do anything more than experimentation, one-off scripts, or "
                "prototypes, then you should use a `Client` instance.",
                "HTTPX clients should be used for anything more than experimentation, "
                "one-off scripts, or prototypes.",
            ),
            WatchedAssertion(
                "0.26.0",
                "If you do anything more than experimentation, one-off scripts, or "
                "prototypes, then you should use a `Client` instance.",
                "HTTPX clients should be used for anything more than experimentation, "
                "one-off scripts, or prototypes.",
            ),
        ),
    ),
    BenchmarkDoc(
        doc_id="async",
        question="How do I run HTTPX requests concurrently with asyncio?",
        query="async await concurrency gather client",
        watched=(
            WatchedAssertion(
                "0.24.1",
                "AnyIO is an [asynchronous networking and concurrency library]"
                "(https://anyio.readthedocs.io/) that works on top of either "
                "`asyncio` or `trio`.",
                "HTTPX async support uses AnyIO, which works on top of either "
                "asyncio or trio.",
            ),
            WatchedAssertion(
                "0.25.2",
                "AnyIO is an [asynchronous networking and concurrency library]"
                "(https://anyio.readthedocs.io/) that works on top of either "
                "`asyncio` or `trio`.",
                "HTTPX async support uses AnyIO, which works on top of either "
                "asyncio or trio.",
            ),
            WatchedAssertion(
                "0.27.2",
                "AnyIO is an [asynchronous networking and concurrency library]"
                "(https://anyio.readthedocs.io/) that works on top of either "
                "`asyncio` or `trio`.",
                "HTTPX async support uses AnyIO, which works on top of either "
                "asyncio or trio.",
            ),
        ),
    ),
    BenchmarkDoc(
        doc_id="compatibility",
        question="How does HTTPX differ from requests for SSL configuration?",
        query="requests migration compatibility differences ssl",
        watched=(
            WatchedAssertion(
                "0.24.1",
                "Requests supports `REQUESTS_CA_BUNDLE` which points to either a "
                "file or a directory.",
                "Requests supports the REQUESTS_CA_BUNDLE environment variable, "
                "which points to either a file or a directory.",
            ),
            WatchedAssertion(
                "0.25.2",
                "Requests supports `REQUESTS_CA_BUNDLE` which points to either a "
                "file or a directory.",
                "Requests supports the REQUESTS_CA_BUNDLE environment variable, "
                "which points to either a file or a directory.",
            ),
            WatchedAssertion(
                "0.26.0",
                "Requests supports `REQUESTS_CA_BUNDLE` which points to either a "
                "file or a directory.",
                "Requests supports the REQUESTS_CA_BUNDLE environment variable, "
                "which points to either a file or a directory.",
            ),
            WatchedAssertion(
                "0.27.2",
                "Requests supports `REQUESTS_CA_BUNDLE` which points to either a "
                "file or a directory.",
                "Requests supports the REQUESTS_CA_BUNDLE environment variable, "
                "which points to either a file or a directory.",
            ),
            WatchedAssertion(
                "0.28.1",
                "If you need more than one different SSL configuration, you should "
                "use different client instances for each SSL configuration.",
                "HTTPX uses different client instances for each different SSL "
                "configuration.",
            ),
        ),
    ),
    BenchmarkDoc(
        doc_id="environment_variables",
        question="Which environment variables does HTTPX read and what do they control?",
        query="environment variables proxy trust_env sslkeylogfile",
        watched=(
            WatchedAssertion(
                "0.24.1",
                "Support for `SSLKEYLOGFILE` requires Python 3.8 and OpenSSL 1.1.1 "
                "or newer.",
                "Support for SSLKEYLOGFILE requires Python 3.8 and OpenSSL 1.1.1 "
                "or newer.",
            ),
            WatchedAssertion(
                "0.27.2",
                "Support for `SSLKEYLOGFILE` requires Python 3.8 and OpenSSL 1.1.1 "
                "or newer.",
                "Support for SSLKEYLOGFILE requires Python 3.8 and OpenSSL 1.1.1 "
                "or newer.",
            ),
            WatchedAssertion(
                "0.28.1",
                "`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` set the proxy to be used "
                "for `http`, `https`, or all requests respectively.",
                "The HTTP_PROXY, HTTPS_PROXY, and ALL_PROXY environment variables "
                "set the proxy used for http, https, or all requests.",
            ),
        ),
    ),
    BenchmarkDoc(
        doc_id="index",
        question="Which Python version does HTTPX require?",
        query="requires python installation pip",
        watched=(
            WatchedAssertion(
                "0.24.1",
                "HTTPX requires Python 3.7+",
                "HTTPX requires Python 3.7 or later.",
            ),
            WatchedAssertion(
                "0.25.2",
                "HTTPX requires Python 3.8+",
                "HTTPX requires Python 3.8 or later.",
            ),
        ),
    ),
    BenchmarkDoc(
        doc_id="quickstart",
        question="How do I disable automatic response content decoding?",
        query="install httpx client get request quickstart",
        watched=(
            WatchedAssertion(
                "0.24.1",
                "In this case any content encoding that the web server has applied "
                "such as `gzip`, `deflate`, or `brotli` will not be automatically "
                "decoded.",
                "HTTPX will not automatically decode content encodings such as "
                "gzip, deflate, or brotli when you access the raw bytes of the "
                "response.",
            ),
            WatchedAssertion(
                "0.25.2",
                "In this case any content encoding that the web server has applied "
                "such as `gzip`, `deflate`, or `brotli` will not be automatically "
                "decoded.",
                "HTTPX will not automatically decode content encodings such as "
                "gzip, deflate, or brotli when you access the raw bytes of the "
                "response.",
            ),
            WatchedAssertion(
                "0.27.2",
                "In some cases you might want to access the raw bytes on the "
                "response without applying any HTTP content decoding.",
                "You can access the raw bytes on the response without applying any "
                "HTTP content decoding.",
            ),
        ),
    ),
)

# Every revision is a real content transition between published snapshots:
# version steps that share a content hash (no revision happened) are skipped,
# so a revision may jump e.g. 0.25.2 -> 0.27.2 directly. The timeline is
# ordered by the new version's real published_at, ties broken by doc_id.
BENCHMARK_REVISIONS: tuple[BenchmarkRevision, ...] = (
    BenchmarkRevision("advanced", "0.24.1", "0.25.2"),
    BenchmarkRevision("async", "0.24.1", "0.25.2"),
    BenchmarkRevision("compatibility", "0.24.1", "0.25.2"),
    BenchmarkRevision("index", "0.24.1", "0.25.2"),
    BenchmarkRevision("quickstart", "0.24.1", "0.25.2"),
    BenchmarkRevision("advanced", "0.25.2", "0.26.0"),
    BenchmarkRevision("compatibility", "0.25.2", "0.26.0"),
    BenchmarkRevision("async", "0.25.2", "0.27.2"),
    BenchmarkRevision("compatibility", "0.26.0", "0.27.2"),
    BenchmarkRevision("environment_variables", "0.24.1", "0.27.2"),
    BenchmarkRevision("quickstart", "0.25.2", "0.27.2"),
    BenchmarkRevision("compatibility", "0.27.2", "0.28.1"),
    BenchmarkRevision("environment_variables", "0.27.2", "0.28.1"),
)

# The aggregate conclusion watches the two real Python-floor facts so one
# revision can invalidate a conclusion spanning documents.
AGGREGATE_CONCLUSION_KEY = "python_floor_claims_supported"
AGGREGATE_WATCHED_DOCS = ("index", "environment_variables")


def docs_by_id() -> dict[str, BenchmarkDoc]:
    return {doc.doc_id: doc for doc in BENCHMARK_DOCS}


def validate_plan(corpus: LocalCorpusProvider) -> None:
    """Fail fast when a pinned quote is not unique in its pinned version."""
    for doc in BENCHMARK_DOCS:
        for assertion in doc.watched:
            try:
                content = corpus.fetch(doc.doc_id, assertion.version_id).content
            except KeyError as exc:
                raise BenchmarkError(
                    "unknown_corpus_version",
                    f"plan references {doc.doc_id!r}@{assertion.version_id!r}, "
                    "which the corpus does not provide",
                ) from exc
            occurrences = content.count(assertion.quote)
            if occurrences != 1:
                raise BenchmarkError(
                    "quote_not_in_corpus",
                    f"planned quote for {doc.doc_id!r}@{assertion.version_id!r} "
                    f"occurs {occurrences} times (must be exactly once): "
                    f"{assertion.quote[:60]!r}...",
                )
    known = {doc.doc_id for doc in BENCHMARK_DOCS}
    for revision in BENCHMARK_REVISIONS:
        if revision.doc_id not in known:
            raise BenchmarkError(
                "unknown_benchmark_doc",
                f"revision references {revision.doc_id!r}, which has no plan entry",
            )


def _recording(corpus: LocalCorpusProvider, doc: BenchmarkDoc, as_of: str) -> FixtureLLM:
    """Deterministic provider for one extraction run of one document."""
    responses: dict[str, str] = {}
    for result in corpus.search(doc.query, top_k=TOP_K, as_of=as_of):
        document = corpus.fetch(result.doc_id, result.version_id)
        if result.doc_id == doc.doc_id:
            watched = doc.watched_for(result.version_id)
            payload = {
                "assertions": [
                    {
                        "statement": watched.statement,
                        "relation": "supports",
                        "quote": watched.quote,
                    }
                ]
            }
        else:
            payload = {"assertions": []}
        responses[
            fixture_key(EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt(doc.question, document))
        ] = json.dumps(payload, ensure_ascii=False)
    return FixtureLLM(responses, model_id="m1-5b-benchmark-model")


def _extract(
    corpus: LocalCorpusProvider, doc: BenchmarkDoc, as_of: str, reasoned_at: str
) -> ExtractionCandidateBundle:
    pipeline = ResearchExtractionPipeline(
        corpus, _recording(corpus, doc, as_of), source_namespace=corpus.corpus_id
    )
    return pipeline.run(
        query=doc.query, question=doc.question, reasoned_at=reasoned_at, top_k=TOP_K, as_of=as_of
    )


def _snapshot_hash(repository: SQLiteRepository) -> str:
    state = {
        "claims": [claim.to_dict() for claim in repository.list_claims()],
        "edges": [edge.to_dict() for edge in repository.list_dependency_edges()],
    }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def full_recompute_state(repository: SQLiteRepository) -> dict[str, dict[str, str]]:
    """Evaluate every claim and conclusion from scratch (the P0 oracle)."""
    claim_states = {
        claim.claim_id: evaluate_claim(repository, claim.claim_id).assessment.value
        for claim in repository.list_claims()
    }
    conclusion_states = {
        conclusion.conclusion_key: evaluate_conclusion(repository, conclusion).outcome.value
        for conclusion in repository.list_current_conclusions()
    }
    return {"claims": claim_states, "conclusions": conclusion_states}


def assert_equivalent(
    current: dict[str, dict[str, str]], recomputed: dict[str, dict[str, str]]
) -> None:
    """Raise unless the stored state matches the full recompute exactly."""
    for section in ("claims", "conclusions"):
        if current[section] != recomputed[section]:
            drifted = {
                node: (current[section].get(node), recomputed[section].get(node))
                for node in sorted(set(current[section]) | set(recomputed[section]))
                if current[section].get(node) != recomputed[section].get(node)
            }
            raise BenchmarkError(
                "equivalence_violation",
                f"stored {section} diverge from full recompute: {drifted}",
            )


def _ordered_timeline(corpus: LocalCorpusProvider) -> list[tuple[BenchmarkRevision, str]]:
    """Revisions sorted by the new version's real published_at, then doc_id."""
    ordered = []
    for revision in BENCHMARK_REVISIONS:
        published_at = corpus.fetch(revision.doc_id, revision.new_version_id).published_at or ""
        ordered.append((revision, published_at))
    return sorted(ordered, key=lambda item: (item[1], item[0].doc_id))


def run_benchmark(corpus_root: str | Path, database_path: str | Path) -> dict[str, Any]:
    """Build T0, apply the real revision timeline, and return the summary."""
    corpus = LocalCorpusProvider(corpus_root)
    validate_plan(corpus)
    docs = docs_by_id()

    repository = SQLiteRepository(database_path)
    try:
        bridge = GraphBridge(repository, corpus)
        watched_claim_ids: dict[str, str] = {}
        t0_evidence = 0
        t0_edges = 0
        for doc in sorted(docs.values(), key=lambda item: item.doc_id):
            bundle = _extract(corpus, doc, T0_AS_OF, T0_REASONED_AT)
            bridge.load_bundle(bundle, observed_at=T0_REASONED_AT)
            t0_evidence += len(bundle.evidence_spans)
            t0_edges += len(bundle.edges)
            watched = doc.watched_for("0.24.1")
            key = derive_canonical_key(watched.statement)
            watched_claim_ids[doc.doc_id] = next(
                claim.claim_id
                for claim in repository.list_claims()
                if claim.canonical_key == key
            )
        assessments = bridge.record_initial_assessments(
            snapshot_id=f"{SCENARIO_ID}:T0", rule_version=RULE_VERSION, reasoned_at=T0_REASONED_AT
        )
        if len(assessments) != len(BENCHMARK_DOCS):
            raise BenchmarkError(
                "unexpected_t0_state",
                f"expected {len(BENCHMARK_DOCS)} initial assessments, got {len(assessments)}",
            )
        for doc in sorted(docs.values(), key=lambda item: item.doc_id):
            bridge.record_initial_conclusion(
                conclusion_key=f"{doc.doc_id}_fact_supported",
                claim_ids=(watched_claim_ids[doc.doc_id],),
                pass_statement=f"The {doc.doc_id} fact is supported by current sources.",
                fail_statement=(
                    f"The {doc.doc_id} fact is no longer supported; re-research required."
                ),
                rule_version=RULE_VERSION,
                reasoned_at=T0_REASONED_AT,
            )
        bridge.record_initial_conclusion(
            conclusion_key=AGGREGATE_CONCLUSION_KEY,
            claim_ids=tuple(watched_claim_ids[doc] for doc in AGGREGATE_WATCHED_DOCS),
            pass_statement=(
                "The documented Python version floors for HTTPX runtime and SSL "
                "debugging support are backed by current sources."
            ),
            fail_statement=(
                "At least one documented Python version floor is no longer "
                "supported; re-research required."
            ),
            rule_version=RULE_VERSION,
            reasoned_at=T0_REASONED_AT,
        )
        t0_counts = repository.entity_counts()
        engine = EvolutionEngine(repository)

        events: list[dict[str, Any]] = []
        selective_total = 0
        full_total = 0
        semantic_changes_total = 0
        rechecked_total = 0
        conclusion_versions_created = 0
        equivalent_every_event = True
        for seq, (revision, published_at) in enumerate(_ordered_timeline(corpus), start=1):
            doc = docs[revision.doc_id]
            old_watched = doc.watched_for(revision.old_version_id)
            new_watched = doc.watched_for(revision.new_version_id)
            bundle_t1 = _extract(corpus, doc, published_at, published_at)
            event, new_source = bridge.revision_event(
                revision.doc_id,
                revision.old_version_id,
                revision.new_version_id,
                project_id=PROJECT_ID,
            )
            package = ChangePackage(
                scenario_id=SCENARIO_ID,
                scenario_version=SCENARIO_VERSION,
                input_snapshot_id=f"{SCENARIO_ID}:T0",
                input_snapshot_hash=_snapshot_hash(repository),
                rule_version=RULE_VERSION,
                event=event,
                new_source=new_source,
                new_claims=bundle_t1.claims,
                new_evidence=bundle_t1.evidence_spans,
                new_edges=bundle_t1.edges,
            )
            run = engine.apply(package)
            run_is_new = all(
                record["change_event_id"] != event.change_event_id for record in events
            )
            current = {
                "claims": {
                    claim.claim_id: (
                        repository.get_current_assessment(claim.claim_id).assessment.value
                    )
                    for claim in repository.list_claims()
                },
                "conclusions": {
                    conclusion.conclusion_key: conclusion.outcome.value
                    for conclusion in repository.list_current_conclusions()
                },
            }
            recomputed = full_recompute_state(repository)
            assert_equivalent(current, recomputed)
            if not run_is_new:
                continue
            claim_evals = len(run.reverification_results)
            conclusion_evals = len(run.recomputed_conclusions)
            semantic_changes = sum(
                1
                for item in run.confirmed_invalidations
                if item["node_key"].startswith("claim:")
            )
            versions_created = len(run.created_conclusions)
            full_size = len(current["claims"]) + len(current["conclusions"])
            selective_total += claim_evals + conclusion_evals
            full_total += full_size
            semantic_changes_total += semantic_changes
            rechecked_total += len(run.rechecked_unchanged)
            conclusion_versions_created += versions_created
            events.append(
                {
                    "seq": seq,
                    "change_event_id": event.change_event_id,
                    "doc_id": revision.doc_id,
                    "old_version_id": revision.old_version_id,
                    "new_version_id": revision.new_version_id,
                    "effective_at": event.effective_at,
                    "kind": (
                        "watched_fact_removed"
                        if new_watched.quote != old_watched.quote
                        else "survival_revision"
                    ),
                    "claims_reassessed": claim_evals,
                    "rechecked_unchanged": len(run.rechecked_unchanged),
                    "semantic_claim_changes": semantic_changes,
                    "conclusions_recomputed": conclusion_evals,
                    "conclusion_versions_created": versions_created,
                    "untouched_nodes": len(run.untouched_nodes),
                    "selective_evaluations": claim_evals + conclusion_evals,
                    "full_recompute_evaluations": full_size,
                    "equivalent": True,
                }
            )

        final_counts = repository.entity_counts()
        final_outcomes = current["conclusions"]
        watched_states = {
            doc_id: current["claims"][claim_id]
            for doc_id, claim_id in sorted(watched_claim_ids.items())
        }
    finally:
        repository.close()

    summary: dict[str, Any] = {
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "corpus_id": corpus.corpus_id,
        "scenario_id": SCENARIO_ID,
        "rule_version": RULE_VERSION,
        "t0": {
            "as_of": T0_AS_OF,
            "source_versions": t0_counts["source_versions"],
            "claims": t0_counts["claims"],
            "evidence_spans": t0_counts["evidence_spans"],
            "dependency_edges": t0_counts["dependency_edges"],
            "conclusions": t0_counts["conclusion_versions"],
        },
        "timeline": {
            "event_count": len(events),
            "events": events,
        },
        "aggregate": {
            "semantic_claim_changes": semantic_changes_total,
            "rechecked_unchanged_claims": rechecked_total,
            "conclusion_recomputations": sum(
                event["conclusions_recomputed"] for event in events
            ),
            "conclusion_versions_created": conclusion_versions_created,
            "selective_evaluations": selective_total,
            "full_recompute_evaluations": full_total,
            "cost_ratio": (selective_total / full_total) if full_total else 0.0,
            "equivalent_at_every_event": equivalent_every_event,
        },
        "final_state": {
            "claims": final_counts["claims"],
            "evidence_spans": final_counts["evidence_spans"],
            "dependency_edges": final_counts["dependency_edges"],
            "conclusion_outcomes": final_outcomes,
            "watched_claim_states": watched_states,
        },
    }
    return summary


def write_summary(summary: dict[str, Any], output_path: str | Path) -> Path:
    """Write the hashed benchmark summary (canonical JSON + output_hash)."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(summary)
    output_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    path.write_text(
        json.dumps({**payload, "output_hash": output_hash}, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    project_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Run the scaled M1-5B evolution benchmark over real corpus history"
    )
    parser.add_argument(
        "--corpus-root", type=Path, default=project_root / "datasets" / "corpus" / "httpx-docs"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "artifacts" / "evolution" / "m1-5b-benchmark" / "summary.json",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="veritas-benchmark-") as tmp:
        summary = run_benchmark(args.corpus_root, Path(tmp) / "benchmark.sqlite3")
    path = write_summary(summary, args.output)
    print(
        json.dumps(
            {
                "benchmark_id": summary["benchmark_id"],
                "events": summary["timeline"]["event_count"],
                "selective_evaluations": summary["aggregate"]["selective_evaluations"],
                "full_recompute_evaluations": summary["aggregate"]["full_recompute_evaluations"],
                "cost_ratio": summary["aggregate"]["cost_ratio"],
                "equivalent_at_every_event": summary["aggregate"]["equivalent_at_every_event"],
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
