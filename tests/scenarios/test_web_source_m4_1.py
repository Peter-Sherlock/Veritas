"""M4-1 scenario: a versioned web source drives the evolution loop (D-048).

A URL is fetched through an injected transport; the fetch ledger's
timeline materializes into the frozen corpus layout; the extraction
pipeline researches the fetched document and the graph bridge registers
the web source (``webwatch:<slug>@f1``). A second fetch observes changed
content (``f2``): ``detect_web_drift`` flags it against the last
observation, the bridge builds a real ``revise`` ChangeEvent between the
two ledger versions, and the P0 engine flips the watched claim to
unsupported and the conclusion pass@1 -> unknown@2.

The whole chain is deterministic: same fetches at the same instants
replay to the same ledger state, and re-materialization is
byte-identical.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from veritas.autonomy import detect_web_drift
from veritas.domain.enums import Assessment, ConclusionOutcome
from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
)
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.integration import GraphBridge
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.sources import (
    WebSourceStore,
    fetch_web_source,
    materialize_corpus,
    url_slug,
)
from veritas.storage.sqlite import SQLiteRepository


URL = "https://example.com/status/retries"
V1 = "The service retries failed requests up to 3 times.\n"
V2 = "The service retries failed requests up to 5 times.\n"
QUOTE = "retries failed requests up to 3 times"
QUESTION = "How many times does the service retry failed requests?"
QUERY = "retries failed requests"
FOUNDER_STATEMENT = "The service retries failed requests up to 3 times."
CONCLUSION_KEY = "web_retry_fact_supported"
CORPUS_ID = "webwatch"
RULE_VERSION = "p0-rules-2"
T1 = "2026-09-02T10:00:00Z"
T2 = "2026-09-02T11:00:00Z"
T3 = "2026-09-02T12:00:00Z"


class ScriptedTransport:
    """Two-version web page served from memory, mutable on revision."""

    def __init__(self) -> None:
        self.pages: dict[str, bytes] = {URL: V1.encode("utf-8")}

    def __call__(self, url: str) -> tuple[int, bytes]:
        return 200, self.pages[url]


def _load_t0(provider: LocalCorpusProvider, repository: SQLiteRepository) -> str:
    slug = url_slug(URL)
    document = provider.fetch(slug, "f1")
    responses = {
        fixture_key(
            EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt(QUESTION, document)
        ): json.dumps(
            {
                "assertions": [
                    {
                        "statement": FOUNDER_STATEMENT,
                        "relation": "supports",
                        "quote": QUOTE,
                    }
                ]
            },
            ensure_ascii=False,
        )
    }
    pipeline = ResearchExtractionPipeline(
        provider, FixtureLLM(responses, model_id="m41-model"), source_namespace=CORPUS_ID
    )
    bundle = pipeline.run(query=QUERY, question=QUESTION, reasoned_at=T1, top_k=3)
    bridge = GraphBridge(repository, provider)
    bridge.load_bundle(bundle, observed_at=T1)
    bridge.record_initial_assessments(
        snapshot_id="M41:T0", rule_version=RULE_VERSION, reasoned_at=T1
    )
    claim_id = next(claim.claim_id for claim in repository.list_claims())
    bridge.record_initial_conclusion(
        conclusion_key=CONCLUSION_KEY,
        claim_ids=(claim_id,),
        pass_statement="The retry fact is supported by the fetched web source.",
        fail_statement="The retry fact is no longer supported; re-research required.",
        rule_version=RULE_VERSION,
        reasoned_at=T1,
    )
    return claim_id


def _snapshot_hash(repository: SQLiteRepository) -> str:
    import hashlib

    state = {
        "claims": [claim.to_dict() for claim in repository.list_claims()],
        "edges": [edge.to_dict() for edge in repository.list_dependency_edges()],
    }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WebSourceEvolutionTests(unittest.TestCase):
    def test_fetched_source_drives_research_drift_and_recompute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WebSourceStore(root / "web.sqlite3")
            repository = SQLiteRepository(root / "evolution.db")
            transport = ScriptedTransport()
            try:
                outcome = fetch_web_source(
                    store, URL, observed_at=T1, transport=transport
                )
                self.assertEqual("new_version", outcome.status)
                self.assertEqual("f1", outcome.version_label)
                slug = url_slug(URL)
                self.assertEqual(slug, outcome.doc_slug)

                corpus_root = materialize_corpus(
                    store, root / "corpus", corpus_id=CORPUS_ID
                )
                provider = LocalCorpusProvider(corpus_root)
                claim_id = _load_t0(provider, repository)
                conclusion = next(iter(repository.list_current_conclusions()))
                self.assertEqual(CONCLUSION_KEY, conclusion.conclusion_key)
                self.assertEqual(ConclusionOutcome.PASS, conclusion.outcome)
                self.assertEqual(1, conclusion.version_number)

                transport.pages[URL] = V2.encode("utf-8")
                changed = fetch_web_source(
                    store, URL, observed_at=T3, transport=transport
                )
                self.assertEqual("new_version", changed.status)
                self.assertEqual("f2", changed.version_label)

                materialize_corpus(store, root / "corpus", corpus_id=CORPUS_ID)
                updated_provider = LocalCorpusProvider(corpus_root)
                drifts = detect_web_drift(repository, store, corpus_id=CORPUS_ID)
                self.assertEqual(1, len(drifts))
                self.assertEqual(slug, drifts[0].doc_id)
                self.assertEqual("f1", drifts[0].current_version)
                self.assertEqual("f2", drifts[0].latest_version)

                bridge = GraphBridge(repository, updated_provider)
                event, new_source = bridge.revision_event(
                    slug, "f1", "f2", project_id="m41-web", observed_at=T3
                )
                self.assertEqual(
                    f"CHG_{slug.upper()}_f1_TO_f2", event.change_event_id
                )
                self.assertNotEqual(
                    new_source.content_hash,
                    repository.get_source_version(f"{CORPUS_ID}:{slug}@f1").content_hash,
                )
                EvolutionEngine(repository).apply(
                    ChangePackage(
                        scenario_id="web-watch",
                        scenario_version="1.0.0",
                        input_snapshot_id=f"web-watch:{slug}",
                        input_snapshot_hash=_snapshot_hash(repository),
                        rule_version=RULE_VERSION,
                        event=event,
                        new_source=new_source,
                        new_claims=(),
                        new_evidence=(),
                        new_edges=(),
                    )
                )
                self.assertEqual(
                    Assessment.UNSUPPORTED,
                    repository.get_current_assessment(claim_id).assessment,
                )
                conclusion = next(iter(repository.list_current_conclusions()))
                self.assertEqual(ConclusionOutcome.UNKNOWN, conclusion.outcome)
                self.assertEqual(2, conclusion.version_number)
                self.assertIn("re-research required", conclusion.statement)
            finally:
                repository.close()
                store.close()

    def test_web_drift_skips_unchanged_and_same_content_fetches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WebSourceStore(root / "web.sqlite3")
            repository = SQLiteRepository(root / "evolution.db")
            transport = ScriptedTransport()
            try:
                fetch_web_source(store, URL, observed_at=T1, transport=transport)
                corpus_root = materialize_corpus(
                    store, root / "corpus", corpus_id=CORPUS_ID
                )
                provider = LocalCorpusProvider(corpus_root)
                bridge = GraphBridge(repository, provider)
                slug = url_slug(URL)
                bridge.register_source_version(slug, "f1", observed_at=T1)

                # A SAME observation changes nothing; registered sources
                # stay current with the ledger.
                fetch_web_source(store, URL, observed_at=T2, transport=transport)
                self.assertEqual([], detect_web_drift(repository, store, corpus_id=CORPUS_ID))
            finally:
                repository.close()
                store.close()

    def test_full_chain_replays_to_the_same_ledger_state(self) -> None:
        """Same fetches at pinned instants reproduce the ledger exactly."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def run_chain(name: str) -> dict[str, object]:
                store = WebSourceStore(root / f"{name}.sqlite3")
                transport = ScriptedTransport()
                try:
                    first = fetch_web_source(
                        store, URL, observed_at=T1, transport=transport
                    )
                    same = fetch_web_source(
                        store, URL, observed_at=T2, transport=transport
                    )
                    transport.pages[URL] = V2.encode("utf-8")
                    changed = fetch_web_source(
                        store, URL, observed_at=T3, transport=transport
                    )
                    return {
                        "first": first,
                        "same": same,
                        "changed": changed,
                        "counts": store.counts(),
                        "latest_hash": store.latest(URL).content_hash,
                        "versions": [v.version_label for v in store.versions(URL)],
                    }
                finally:
                    store.close()

            one = run_chain("chain-a")
            two = run_chain("chain-b")
            self.assertEqual(one["counts"], two["counts"])
            self.assertEqual(one["versions"], two["versions"])
            self.assertEqual(one["latest_hash"], two["latest_hash"])
            for key in ("first", "same", "changed"):
                self.assertEqual(getattr(one[key], "status"), getattr(two[key], "status"))
                self.assertEqual(
                    getattr(one[key], "version_label"),
                    getattr(two[key], "version_label"),
                )


if __name__ == "__main__":
    unittest.main()
