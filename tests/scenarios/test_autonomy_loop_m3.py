"""M3 scenario: the autonomous research loop repairs churn (D-042).

Full arc on a real local corpus with one real revision:

1. T0 research (clustered) establishes the claim and a PASS conclusion;
2. the revision-era re-research runs on a pre-M2 path without clustering,
   so the model's rewording churns the graph: the claim goes unsupported
   and the conclusion flips to unknown;
3. the planner turns the non-PASS conclusion into a deterministic
   runtime session spec;
4. the re-research session runs clustered, and the refresh applier
   re-enters the new evidence into the graph: the claim returns to
   ACCEPTED and the conclusion recomputes back to PASS (v3);
5. re-applying the refresh is a no-op (idempotency).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.aggregation import ClaimClusterStore
from veritas.aggregation.resolve import resolve_bundle
from veritas.autonomy import (
    RefreshError,
    apply_research_refresh,
    plan_re_research,
)
from veritas.domain.enums import Assessment, ConclusionOutcome
from veritas.extraction.models import ExtractionCandidateBundle
from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
)
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.integration import GraphBridge
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


QUESTION = "Does HTTPX retry failed connection setups?"
QUERY = "retries connection setup failures"
TOP_K = 3
PROJECT_ID = "m3-retry-research"
RULE_VERSION = "p0-rules-2"
CONCLUSION_KEY = "retry_fact_supported"

V1_CONTENT = "HTTPX retries connection setup failures by default.\n"
V2_CONTENT = (
    "HTTPX retries connection setup failures by default.\n"
    "\n"
    "See the retry policy documentation for the full behavior.\n"
)
QUOTE = "HTTPX retries connection setup failures"
FOUNDER_STATEMENT = "HTTPX retries connection setup failures."
PARAPHRASE_STATEMENT = "HTTPX automatically retries connection setup failures."

T0_AS_OF = "2026-01-15T00:00:00Z"
T1_AS_OF = "2026-02-15T00:00:00Z"
REASONED_AT = "2026-08-30T00:00:00Z"
REFRESHED_AT = "2026-08-30T01:00:00Z"
SESSION_ID = "m3-loop"


def _build_corpus(root: Path) -> LocalCorpusProvider:
    (root / "retries").mkdir(parents=True)
    versions = []
    for version_id, content, published_at in (
        ("1.0", V1_CONTENT, "2026-01-01T00:00:00Z"),
        ("2.0", V2_CONTENT, "2026-02-01T00:00:00Z"),
    ):
        (root / "retries" / f"{version_id}.md").write_text(content, encoding="utf-8", newline="\n")
        versions.append(
            {
                "version_id": version_id,
                "path": f"retries/{version_id}.md",
                "published_at": published_at,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "source_ref": f"fixture:{version_id}",
            }
        )
    manifest = {
        "corpus_id": "m3-corpus",
        "documents": [
            {"doc_id": "retries", "title": "retries", "versions": versions}
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return LocalCorpusProvider(root)


def _recording(corpus: LocalCorpusProvider, as_of: str) -> FixtureLLM:
    responses: dict[str, str] = {}
    for result in corpus.search(QUERY, top_k=TOP_K, as_of=as_of):
        document = corpus.fetch(result.doc_id, result.version_id)
        if document.doc_id == "retries":
            statement = (
                FOUNDER_STATEMENT if document.version_id == "1.0" else PARAPHRASE_STATEMENT
            )
            payload = {
                "assertions": [
                    {"statement": statement, "relation": "supports", "quote": QUOTE}
                ]
            }
        else:
            payload = {"assertions": []}
        responses[
            fixture_key(EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt(QUESTION, document))
        ] = json.dumps(payload, ensure_ascii=False)
    return FixtureLLM(responses, model_id="m3-model")


def _extract(corpus: LocalCorpusProvider, as_of: str):
    pipeline = ResearchExtractionPipeline(
        corpus, _recording(corpus, as_of), source_namespace=corpus.corpus_id
    )
    return pipeline.run(
        query=QUERY, question=QUESTION, reasoned_at=REASONED_AT, top_k=TOP_K, as_of=as_of
    )


def _snapshot_hash(repository: SQLiteRepository) -> str:
    state = {
        "claims": [claim.to_dict() for claim in repository.list_claims()],
        "edges": [edge.to_dict() for edge in repository.list_dependency_edges()],
    }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AutonomyLoopM3Tests(unittest.TestCase):
    def test_loop_repairs_churn_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _build_corpus(Path(tmp) / "corpus")
            repository = SQLiteRepository(Path(tmp) / "evolution.db")
            clusters = ClaimClusterStore(Path(tmp) / "clusters.sqlite3")
            try:
                # 1. T0 research, clustered: claim accepted, conclusion PASS.
                bridge = GraphBridge(repository, corpus)
                bundle_t0 = resolve_bundle(
                    _extract(corpus, T0_AS_OF), clusters, observed_at=REASONED_AT
                )
                bridge.load_bundle(bundle_t0, observed_at=REASONED_AT)
                bridge.record_initial_assessments(
                    snapshot_id="M3:T0", rule_version=RULE_VERSION, reasoned_at=REASONED_AT
                )
                old_claim_id = next(claim.claim_id for claim in repository.list_claims())
                bridge.record_initial_conclusion(
                    conclusion_key=CONCLUSION_KEY,
                    claim_ids=(old_claim_id,),
                    pass_statement="The retry fact is supported by current sources.",
                    fail_statement="The retry fact is no longer supported; re-research required.",
                    rule_version=RULE_VERSION,
                    reasoned_at=REASONED_AT,
                )
                self.assertEqual(
                    ConclusionOutcome.PASS,
                    repository.get_current_conclusion(CONCLUSION_KEY).outcome,
                )

                # 2. The revision-era re-research ran unclustered (pre-M2
                # path): the rewording churns claim and conclusion.
                event, new_source = bridge.revision_event(
                    "retries", "1.0", "2.0", project_id=PROJECT_ID
                )
                bundle_churn = _extract(corpus, T1_AS_OF)
                run = EvolutionEngine(repository).apply(
                    ChangePackage(
                        scenario_id="M3",
                        scenario_version="1.0.0",
                        input_snapshot_id="M3:T0",
                        input_snapshot_hash=_snapshot_hash(repository),
                        rule_version=RULE_VERSION,
                        event=event,
                        new_source=new_source,
                        new_claims=bundle_churn.claims,
                        new_evidence=bundle_churn.evidence_spans,
                        new_edges=bundle_churn.edges,
                    )
                )
                self.assertEqual(
                    Assessment.UNSUPPORTED,
                    repository.get_current_assessment(old_claim_id).assessment,
                )
                self.assertEqual(
                    ConclusionOutcome.UNKNOWN,
                    repository.get_current_conclusion(CONCLUSION_KEY).outcome,
                )

                # 3. The planner turns the non-PASS conclusion into a
                # runtime session spec targeting the watched claim's fact.
                plan = plan_re_research(repository, session_id=SESSION_ID)
                self.assertEqual(1, len(plan.items))
                item = plan.items[0]
                self.assertEqual("httpx retries connection setup failures", item.query)
                self.assertEqual(FOUNDER_STATEMENT, item.question)
                self.assertEqual(3, plan.budget_requests)
                spec = plan.to_spec()
                self.assertEqual(SESSION_ID, spec["session_id"])

                # 4. The clustered re-research session re-enters the fact;
                # the refresh applier repairs claim and conclusion.
                bundle_re = resolve_bundle(
                    _extract(corpus, T1_AS_OF), clusters, observed_at=REFRESHED_AT
                )
                self.assertEqual(
                    old_claim_id, bundle_re.claims[0].claim_id
                )
                payload = apply_research_refresh(
                    repository,
                    bundle=bundle_re,
                    session_id=SESSION_ID,
                    rule_version=RULE_VERSION,
                    refreshed_at=REFRESHED_AT,
                )
                self.assertIn(old_claim_id, payload["semantic_changed_claims"])
                self.assertIn(CONCLUSION_KEY, payload["recomputed_conclusions"])
                self.assertEqual(
                    Assessment.ACCEPTED,
                    repository.get_current_assessment(old_claim_id).assessment,
                )
                repaired = repository.get_current_conclusion(CONCLUSION_KEY)
                self.assertEqual(3, repaired.version_number)
                self.assertEqual(ConclusionOutcome.PASS, repaired.outcome)

                # 5. Re-applying the same refresh is a no-op.
                counts = repository.entity_counts()
                replayed = apply_research_refresh(
                    repository,
                    bundle=bundle_re,
                    session_id=SESSION_ID,
                    rule_version=RULE_VERSION,
                    refreshed_at=REFRESHED_AT,
                )
                self.assertEqual(payload, replayed)
                self.assertEqual(counts, repository.entity_counts())
            finally:
                clusters.close()
                repository.close()

    def test_refresh_rejects_evidence_grounded_in_inactive_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _build_corpus(Path(tmp) / "corpus")
            repository = SQLiteRepository(Path(tmp) / "evolution.db")
            clusters = ClaimClusterStore(Path(tmp) / "clusters.sqlite3")
            try:
                bridge = GraphBridge(repository, corpus)
                bundle_t0 = resolve_bundle(
                    _extract(corpus, T0_AS_OF), clusters, observed_at=REASONED_AT
                )
                bridge.load_bundle(bundle_t0, observed_at=REASONED_AT)
                bridge.record_initial_assessments(
                    snapshot_id="M3:T0", rule_version=RULE_VERSION, reasoned_at=REASONED_AT
                )
                # Advance the corpus past 1.0: the old source is superseded.
                event, new_source = bridge.revision_event(
                    "retries", "1.0", "2.0", project_id=PROJECT_ID
                )
                stale_bundle = resolve_bundle(
                    _extract(corpus, T0_AS_OF), clusters, observed_at=REFRESHED_AT
                )
                with self.assertRaises(RefreshError) as caught:
                    apply_research_refresh(
                        repository,
                        bundle=stale_bundle,
                        session_id=SESSION_ID,
                        rule_version=RULE_VERSION,
                        refreshed_at=REFRESHED_AT,
                    )
                self.assertEqual("superseded_source", caught.exception.code)
            finally:
                clusters.close()
                repository.close()

    def test_refresh_rejects_empty_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "evolution.db")
            try:
                with self.assertRaises(RefreshError) as caught:
                    apply_research_refresh(
                        repository,
                        bundle=ExtractionCandidateBundle(
                            query="q",
                            question="q?",
                            retrieved=(),
                            documents=(),
                            evidence_spans=(),
                            claims=(),
                            edges=(),
                        ),
                        session_id=SESSION_ID,
                        rule_version=RULE_VERSION,
                        refreshed_at=REFRESHED_AT,
                    )
                self.assertEqual("empty_refresh_bundle", caught.exception.code)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
