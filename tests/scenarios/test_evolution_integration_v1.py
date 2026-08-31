from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.evidence.rules import Assessment, ConclusionOutcome
from veritas.extraction.models import ExtractionCandidateBundle
from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
)
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.integration import GraphBridge, GraphBridgeError
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"

QUESTION = "Which Python version does HTTPX require?"
QUERY = "requires python installation pip"
TOP_K = 3
T0_AS_OF = "2023-06-01T00:00:00Z"  # index sits at 0.24.1: "HTTPX requires Python 3.7+"
T1_AS_OF = "2023-12-01T00:00:00Z"  # index sits at 0.25.2: "HTTPX requires Python 3.8+"
T0_REASONED_AT = "2026-08-30T00:00:00Z"
PROJECT_ID = "httpx-research"

OLD_KEY = "httpx_requires_python_3_7_or_later"
NEW_KEY = "httpx_requires_python_3_8_or_later"
CONCLUSION_KEY = "python_floor_claim_supported"
RULE_VERSION = "p0-rules-2"

# Real sentences from the corpus snapshots; each occurs exactly once.
FLOOR_SENTENCES = {
    "0.24.1": ("HTTPX requires Python 3.7+", "HTTPX requires Python 3.7 or later."),
    "0.25.2": ("HTTPX requires Python 3.8+", "HTTPX requires Python 3.8 or later."),
}


def _recording(corpus: LocalCorpusProvider, as_of: str) -> FixtureLLM:
    """Deterministic provider: the floor assertion for index, empty elsewhere."""
    responses: dict[str, str] = {}
    for result in corpus.search(QUERY, top_k=TOP_K, as_of=as_of):
        document = corpus.fetch(result.doc_id, result.version_id)
        if document.doc_id == "index":
            quote, statement = FLOOR_SENTENCES[document.version_id]
            payload = {
                "assertions": [
                    {"statement": statement, "relation": "supports", "quote": quote}
                ]
            }
        else:
            payload = {"assertions": []}
        responses[fixture_key(EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt(QUESTION, document))] = json.dumps(payload, ensure_ascii=False)
    return FixtureLLM(responses, model_id="integration-replay-model")


def _extract(corpus: LocalCorpusProvider, as_of: str) -> ExtractionCandidateBundle:
    pipeline = ResearchExtractionPipeline(
        corpus, _recording(corpus, as_of), source_namespace=corpus.corpus_id
    )
    return pipeline.run(
        query=QUERY, question=QUESTION, reasoned_at=T0_REASONED_AT, top_k=TOP_K, as_of=as_of
    )


def _snapshot_hash(repository: SQLiteRepository) -> str:
    state = {
        "claims": [claim.to_dict() for claim in repository.list_claims()],
        "edges": [edge.to_dict() for edge in repository.list_dependency_edges()],
    }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EvolutionIntegrationV1Tests(unittest.TestCase):
    def test_real_corpus_revision_drives_evolution_end_to_end(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "evolution.db")
            bridge = GraphBridge(repository, corpus)
            try:
                # T0: research at the 0.24.1 era, loaded into the evolution
                # store with assessments and a watching conclusion.
                loaded = bridge.load_bundle(_extract(corpus, T0_AS_OF), observed_at=T0_REASONED_AT)
                self.assertGreaterEqual(loaded["source_versions"], 3)
                self.assertEqual(1, loaded["claims"])
                assessments = bridge.record_initial_assessments(
                    snapshot_id="M1-5A:T0", rule_version=RULE_VERSION, reasoned_at=T0_REASONED_AT
                )
                self.assertEqual(1, len(assessments))
                old_claim_id = next(
                    claim.claim_id
                    for claim in repository.list_claims()
                    if claim.canonical_key == OLD_KEY
                )
                conclusion = bridge.record_initial_conclusion(
                    conclusion_key=CONCLUSION_KEY,
                    claim_ids=(old_claim_id,),
                    pass_statement="The documented Python floor claim is supported by current sources.",
                    fail_statement="The documented Python floor claim is no longer supported; re-research required.",
                    rule_version=RULE_VERSION,
                    reasoned_at=T0_REASONED_AT,
                )
                self.assertEqual("python_floor_claim_supported@1", conclusion.conclusion_version_id)
                self.assertEqual(ConclusionOutcome.PASS, conclusion.outcome)

                # The change: a real revision from the corpus history.
                event, new_source = bridge.revision_event(
                    "index", "0.24.1", "0.25.2", project_id=PROJECT_ID
                )
                self.assertEqual("CHG_INDEX_0.24.1_TO_0.25.2", event.change_event_id)
                self.assertEqual("2023-11-24T16:33:18+04:00", event.effective_at)
                self.assertEqual(
                    "httpx-docs:index@0.24.1", new_source.supersedes_version_id
                )

                # T1: re-research in the 0.25.2 era produces the new-claim package.
                bundle_t1 = _extract(corpus, T1_AS_OF)
                package = ChangePackage(
                    scenario_id="M1-5A",
                    scenario_version="1.0.0",
                    input_snapshot_id="M1-5A:T0",
                    input_snapshot_hash=_snapshot_hash(repository),
                    rule_version=RULE_VERSION,
                    event=event,
                    new_source=new_source,
                    new_claims=bundle_t1.claims,
                    new_evidence=bundle_t1.evidence_spans,
                    new_edges=bundle_t1.edges,
                )
                run = EvolutionEngine(repository).apply(package)

                # The new floor claim is supported; the old one lost its evidence.
                new_claim_id = next(
                    claim.claim_id
                    for claim in repository.list_claims()
                    if claim.canonical_key == NEW_KEY
                )
                self.assertEqual(
                    Assessment.ACCEPTED,
                    repository.get_current_assessment(new_claim_id).assessment,
                )
                self.assertEqual(
                    Assessment.UNSUPPORTED,
                    repository.get_current_assessment(old_claim_id).assessment,
                )
                # Append-only current view: the old evidence rows keep their
                # content, but supersession deactivates every edge grounded
                # in the superseded source version.
                self.assertEqual(
                    [], repository.list_active_evidence_edges_for_claim(old_claim_id)
                )

                # The watching conclusion advanced to v2: pass -> unknown.
                current = repository.get_current_conclusion(CONCLUSION_KEY)
                self.assertEqual(2, current.version_number)
                self.assertEqual(ConclusionOutcome.UNKNOWN, current.outcome)
                self.assertEqual(
                    "The documented Python floor claim is no longer supported; re-research required.",
                    current.statement,
                )
                self.assertEqual(
                    "python_floor_claim_supported@1",
                    current.supersedes_conclusion_version_id,
                )
                self.assertEqual([CONCLUSION_KEY], list(run.recomputed_conclusions))
                self.assertIsNotNone(
                    repository.find_evolution_run(PROJECT_ID, event.external_event_id)
                )

                # Idempotency: re-applying the same package returns the same
                # run and changes nothing.
                counts = repository.entity_counts()
                replayed = EvolutionEngine(repository).apply(package)
                self.assertEqual(run.run_id, replayed.run_id)
                self.assertEqual(counts, repository.entity_counts())
            finally:
                repository.close()

    def test_revision_event_rejects_versions_outside_corpus_history(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "evolution.db")
            bridge = GraphBridge(repository, corpus)
            try:
                bridge.register_source_version("index", "0.24.1", observed_at=T0_REASONED_AT)
                with self.assertRaises(GraphBridgeError) as caught:
                    bridge.revision_event("index", "0.24.1", "9.9.9", project_id=PROJECT_ID)
                self.assertEqual("unknown_corpus_version", caught.exception.code)
            finally:
                repository.close()

    def test_revision_event_rejects_unregistered_old_version(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "evolution.db")
            bridge = GraphBridge(repository, corpus)
            try:
                with self.assertRaises(GraphBridgeError) as caught:
                    bridge.revision_event("index", "0.24.1", "0.25.2", project_id=PROJECT_ID)
                self.assertEqual("unregistered_old_source_version", caught.exception.code)
            finally:
                repository.close()

    def test_load_bundle_rejects_evidence_from_unregistered_sources(self) -> None:
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "evolution.db")
            try:
                # A pipeline with a mismatched namespace produces evidence
                # ids the bridge cannot ground in the corpus.
                mismatched = ResearchExtractionPipeline(
                    corpus, _recording(corpus, T0_AS_OF), source_namespace="other-corpus"
                )
                bundle = mismatched.run(
                    query=QUERY, question=QUESTION, reasoned_at=T0_REASONED_AT, top_k=3,
                    as_of=T0_AS_OF,
                )
                self.assertTrue(bundle.evidence_spans)
                bridge = GraphBridge(repository, corpus)
                with self.assertRaises(GraphBridgeError) as caught:
                    bridge.load_bundle(bundle, observed_at=T0_REASONED_AT)
                self.assertEqual("unregistered_source_version", caught.exception.code)
                self.assertEqual(0, sum(repository.entity_counts().values()))
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
