"""M2-2 scenario: paraphrase re-research survives a real revision (D-041).

The M1-5A/M1-5B survival story only worked because fixture replay repeats
statements verbatim. A real model re-words the fact on the next pass —
under exact canonical keys that rewording fragments the graph: the old
claim goes unsupported, a new claim appears, and the watching conclusion
flips to unknown even though the fact itself never changed (C2 churn).

This scenario drives the same real revision twice over a real local
corpus — once with claim-identity clustering enabled and once without:

* **clustered**: the T1 paraphrase resolves into the T0 claim's cluster,
  the new evidence re-attaches to the same claim, the claim stays
  accepted, and the conclusion stays at v1 pass — zero churn;
* **unclustered**: the paraphrase is a new claim, the old claim loses
  support, and the conclusion advances to v2 unknown.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.aggregation import ClaimClusterStore
from veritas.aggregation.resolve import resolve_bundle
from veritas.domain.enums import Assessment, ConclusionOutcome
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


QUESTION = "Does HTTPX retry failed connection setups?"
QUERY = "retries connection setup failures"
TOP_K = 3
PROJECT_ID = "m2-2-retry-research"
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


def _build_corpus(root: Path) -> LocalCorpusProvider:
    """A real local corpus with two versions of one document."""
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
        "corpus_id": "m2-2-corpus",
        "documents": [
            {"doc_id": "retries", "title": "retries", "versions": versions}
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return LocalCorpusProvider(root)


def _recording(corpus: LocalCorpusProvider, as_of: str) -> FixtureLLM:
    """The model asserts the same fact, re-wording it on the T1 pass."""
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
    return FixtureLLM(responses, model_id="m2-2-model")


def _extract(corpus: LocalCorpusProvider, as_of: str) -> object:
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


class ClusterEvolutionM22Tests(unittest.TestCase):
    def _run_t0(self, corpus: LocalCorpusProvider, repository: SQLiteRepository,
                clusters: ClaimClusterStore | None):
        bundle = _extract(corpus, T0_AS_OF)
        if clusters is not None:
            bundle = resolve_bundle(bundle, clusters, observed_at=REASONED_AT)
        bridge = GraphBridge(repository, corpus)
        bridge.load_bundle(bundle, observed_at=REASONED_AT)
        bridge.record_initial_assessments(
            snapshot_id="M2-2:T0", rule_version=RULE_VERSION, reasoned_at=REASONED_AT
        )
        claim_id = next(claim.claim_id for claim in repository.list_claims())
        conclusion = bridge.record_initial_conclusion(
            conclusion_key=CONCLUSION_KEY,
            claim_ids=(claim_id,),
            pass_statement="The retry fact is supported by current sources.",
            fail_statement="The retry fact is no longer supported; re-research required.",
            rule_version=RULE_VERSION,
            reasoned_at=REASONED_AT,
        )
        return bridge, claim_id, conclusion

    def _apply_revision(self, corpus: LocalCorpusProvider, repository: SQLiteRepository,
                        bridge: GraphBridge, clusters: ClaimClusterStore | None):
        event, new_source = bridge.revision_event(
            "retries", "1.0", "2.0", project_id=PROJECT_ID
        )
        bundle_t1 = _extract(corpus, T1_AS_OF)
        if clusters is not None:
            bundle_t1 = resolve_bundle(bundle_t1, clusters, observed_at=REASONED_AT)
        package = ChangePackage(
            scenario_id="M2-2",
            scenario_version="1.0.0",
            input_snapshot_id="M2-2:T0",
            input_snapshot_hash=_snapshot_hash(repository),
            rule_version=RULE_VERSION,
            event=event,
            new_source=new_source,
            new_claims=bundle_t1.claims,
            new_evidence=bundle_t1.evidence_spans,
            new_edges=bundle_t1.edges,
        )
        return EvolutionEngine(repository).apply(package)

    def test_clustered_paraphrase_survives_revision_without_churn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _build_corpus(Path(tmp) / "corpus")
            with tempfile.TemporaryDirectory() as db_tmp:
                repository = SQLiteRepository(Path(db_tmp) / "evolution.db")
                clusters = ClaimClusterStore(Path(db_tmp) / "clusters.sqlite3")
                try:
                    bridge, claim_id, conclusion = self._run_t0(corpus, repository, clusters)
                    self.assertEqual(ConclusionOutcome.PASS, conclusion.outcome)

                    run = self._apply_revision(corpus, repository, bridge, clusters)

                    # The paraphrase re-entered the SAME claim: the new
                    # evidence re-based it, the state never changed, and
                    # no conclusion was touched.
                    self.assertEqual(
                        Assessment.ACCEPTED,
                        repository.get_current_assessment(claim_id).assessment,
                    )
                    self.assertEqual([claim_id], list(run.rechecked_unchanged))
                    self.assertEqual([], list(run.recomputed_conclusions))
                    current = repository.get_current_conclusion(CONCLUSION_KEY)
                    self.assertEqual(1, current.version_number)
                    self.assertEqual(ConclusionOutcome.PASS, current.outcome)
                    # The T1 rewording joined the T0 founder's cluster.
                    founder_key = derive_canonical_key(FOUNDER_STATEMENT)
                    paraphrase_key = derive_canonical_key(PARAPHRASE_STATEMENT)
                    self.assertEqual(
                        founder_key, clusters.representative_key(paraphrase_key)
                    )
                    self.assertEqual({"clusters": 1, "members": 2}, clusters.counts())
                finally:
                    clusters.close()
                    repository.close()

    def test_unclustered_paraphrase_churns_the_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _build_corpus(Path(tmp) / "corpus")
            with tempfile.TemporaryDirectory() as db_tmp:
                repository = SQLiteRepository(Path(db_tmp) / "evolution.db")
                try:
                    bridge, claim_id, conclusion = self._run_t0(corpus, repository, None)
                    self.assertEqual(ConclusionOutcome.PASS, conclusion.outcome)

                    run = self._apply_revision(corpus, repository, bridge, None)

                    # Without clustering the rewording is a new claim: the
                    # old claim loses support and the conclusion flips.
                    self.assertEqual(
                        Assessment.UNSUPPORTED,
                        repository.get_current_assessment(claim_id).assessment,
                    )
                    current = repository.get_current_conclusion(CONCLUSION_KEY)
                    self.assertEqual(2, current.version_number)
                    self.assertEqual(ConclusionOutcome.UNKNOWN, current.outcome)
                finally:
                    repository.close()


if __name__ == "__main__":
    unittest.main()
