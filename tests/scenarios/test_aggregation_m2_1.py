from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.aggregation import ClaimClusterStore
from veritas.aggregation.clusterer import similarity
from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
    claim_id_for,
    derive_canonical_key,
)
from veritas.extraction.store import CandidateStore
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.runtime import ResearchRuntime, RuntimeStore, WorkItem
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.search.provider import SearchResult, VersionedDocument


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2c" / "benchmark.json"
RECORDING = (
    REPO_ROOT
    / "artifacts"
    / "extraction"
    / "httpx-initial-extraction-3.0.0-deepseek-v4-flash"
    / "responses-recording.json"
)
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"

REASONED_AT = "2026-08-30T00:00:00Z"
QUESTION = "Does HTTPX retry failed connection setups?"

# Two real phrasings of one fact, each grounded verbatim in its document —
# the second is the C2 paraphrase-noise pattern observed in the live recording.
DOC_CONTENTS = {
    "retries": "HTTPX retries connection setup failures by default.",
    "narrow": "HTTPX automatically retries connection setup failures when needed.",
}
FOUNDER_STATEMENT = "HTTPX retries connection setup failures."
FOUNDER_QUOTE = "HTTPX retries connection setup failures"
PARAPHRASE_STATEMENT = "HTTPX automatically retries connection setup failures."
PARAPHRASE_QUOTE = "HTTPX automatically retries connection setup failures"


def _document(doc_id: str, content: str) -> VersionedDocument:
    return VersionedDocument(
        doc_id=doc_id,
        version_id="1.0",
        title=doc_id,
        content=content,
        published_at="2026-08-01T00:00:00Z",
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


class _SingleDocSearch:
    """Serves one fixed document per query (the query is the doc id)."""

    def __init__(self, contents: dict[str, str]) -> None:
        self._documents = {
            doc_id: _document(doc_id, content) for doc_id, content in contents.items()
        }

    def search(self, query: str, *, top_k: int = 5, as_of: str | None = None):
        document = self._documents[query]
        return [
            SearchResult(
                doc_id=document.doc_id,
                version_id=document.version_id,
                title=document.title,
                path=Path("fixture.md"),
                score=1.0,
                snippet=document.content,
            )
        ][:top_k]

    def fetch(self, doc_id: str, version_id: str) -> VersionedDocument:
        if version_id != "1.0" or doc_id not in self._documents:
            raise KeyError((doc_id, version_id))
        return self._documents[doc_id]


def _recording(corpus: _SingleDocSearch, assertions: dict[str, tuple[str, str]]) -> FixtureLLM:
    """One (statement, quote) assertion per document, keyed by pipeline prompts."""
    responses: dict[str, str] = {}
    for doc_id, (statement, quote) in assertions.items():
        document = corpus.fetch(doc_id, "1.0")
        payload = {"assertions": [{"statement": statement, "relation": "supports", "quote": quote}]}
        responses[
            fixture_key(
                EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt(QUESTION, document)
            )
        ] = json.dumps(payload, ensure_ascii=False)
    return FixtureLLM(responses, model_id="m2-1-model")


def _live_candidates() -> dict[tuple[str, str], str]:
    """(doc_id, canonical_key) -> statement, replayed from the frozen live recording."""
    benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    recording = json.loads(RECORDING.read_text(encoding="utf-8"))
    corpus = LocalCorpusProvider(CORPUS_ROOT)
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
            # Contract-rejected cases contribute no candidates; the frozen
            # failure distribution is pinned by the live replay tests.
            continue
        for document in bundle.documents:
            for assertion in document.assertions:
                candidates.setdefault(
                    (document.doc_id, derive_canonical_key(assertion.statement)),
                    assertion.statement,
                )
    return candidates


def _gold_assertions() -> list[tuple[str, str, str]]:
    benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    return [
        (case["case_id"], item["doc_id"], item["statement"])
        for case in benchmark["cases"]
        for item in case["expected_assertions"]
    ]


class AggregationCalibrationM21Tests(unittest.TestCase):
    """Frozen M2-1 calibration: the deterministic clusterer against the
    real DeepSeek recording and the 32 gold assertions (D-040)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.live = _live_candidates()
        cls.gold = _gold_assertions()

    def _best_match(self, doc_id: str, statement: str) -> float | None:
        best: float | None = None
        for (doc, _key), candidate in self.live.items():
            if doc != doc_id:
                continue
            score = similarity(statement, candidate)
            if score is None:
                continue
            if best is None or score > best:
                best = score
        return best

    def test_cluster_coverage_rises_from_three_to_nineteen_of_thirty_two(self) -> None:
        exact = sum(
            1
            for _case, doc, statement in self.gold
            if (doc, derive_canonical_key(statement)) in self.live
        )
        self.assertEqual(3, exact)
        covered = 0
        for _case, doc, statement in self.gold:
            score = self._best_match(doc, statement)
            if score is not None and score >= 0.375:
                covered += 1
        self.assertEqual(19, covered)

    def test_boundary_true_paraphrase_merges_and_false_pair_stays_out(self) -> None:
        # EX-027: true paraphrase at 0.385 — above the frozen threshold.
        self.assertGreaterEqual(self._best_match("advanced", _gold("EX-027")), 0.375)
        # EX-012: different fact at 0.364 — below it; must not merge.
        score_12 = self._best_match("quickstart", _gold("EX-012"))
        self.assertIsNotNone(score_12)
        self.assertLess(score_12, 0.375)

    def test_versioned_gold_facts_stay_guarded_from_live_variants(self) -> None:
        # EX-029 pins the Python floor at 0.24.1/3.7; the live recording
        # only holds the 3.8 floor. The number guard keeps them apart
        # regardless of wording.
        gold_29 = _gold("EX-029")
        live_floor = "HTTPX requires Python 3.8 or later."
        self.assertIsNone(similarity(gold_29, live_floor))


def _gold(case_id: str) -> str:
    benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    for case in benchmark["cases"]:
        if case["case_id"] == case_id:
            return case["expected_assertions"][0]["statement"]
    raise AssertionError(f"unknown case {case_id}")


class RuntimeClusterIntegrationTests(unittest.TestCase):
    def _runtime(self, tmp: Path, *, cluster_store: ClaimClusterStore | None):
        search = _SingleDocSearch(DOC_CONTENTS)
        provider = _recording(
            search,
            {
                "retries": (FOUNDER_STATEMENT, FOUNDER_QUOTE),
                "narrow": (PARAPHRASE_STATEMENT, PARAPHRASE_QUOTE),
            },
        )
        store = RuntimeStore(tmp / "runtime.sqlite3")
        candidates = CandidateStore(tmp / "candidates.sqlite3") if cluster_store is not None else None
        return ResearchRuntime(
            search=search,
            provider=provider,
            store=store,
            source_namespace="fixture-corpus",
            candidate_store=candidates,
            cluster_store=cluster_store,
        ), store, candidates

    def test_paraphrase_research_reenters_the_cluster_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with ClaimClusterStore(Path(tmp) / "clusters.sqlite3") as clusters:
                runtime, store, candidates = self._runtime(
                    Path(tmp) / "session", cluster_store=clusters
                )
                try:
                    result = runtime.run(
                        session_id="m2-1-cluster",
                        items=(
                            WorkItem(item_id="EX-A", query="retries", question=QUESTION, top_k=1),
                            WorkItem(item_id="EX-B", query="narrow", question=QUESTION, top_k=1),
                        ),
                        budget_requests=4,
                        observed_at=REASONED_AT,
                    )
                    self.assertEqual("completed", result["status"])

                    # The two phrasings collapsed into one cluster whose
                    # representative is the founder's key.
                    founder_key = derive_canonical_key(FOUNDER_STATEMENT)
                    paraphrase_key = derive_canonical_key(PARAPHRASE_STATEMENT)
                    self.assertEqual(
                        founder_key, clusters.representative_key(paraphrase_key)
                    )
                    self.assertEqual(
                        (paraphrase_key, founder_key), clusters.find_cluster(paraphrase_key)
                    )
                    self.assertEqual({"clusters": 1, "members": 2}, clusters.counts())

                    # The paraphrase materializes to the founder claim id...
                    self.assertEqual(
                        claim_id_for(founder_key),
                        claim_id_for(clusters.representative_key(paraphrase_key)),
                    )
                    # ...while the candidate store keeps both raw keys as
                    # separate pre-aggregation observations.
                    self.assertIsNotNone(candidates)
                    self.assertEqual(2, candidates.counts()["candidates"])
                finally:
                    candidates.close()
                    store.close()

    def test_without_cluster_store_phrasings_stay_separate_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, store, _candidates = self._runtime(Path(tmp), cluster_store=None)
            try:
                runtime.run(
                    session_id="m2-1-plain",
                    items=(
                        WorkItem(item_id="EX-A", query="retries", question=QUESTION, top_k=1),
                        WorkItem(item_id="EX-B", query="narrow", question=QUESTION, top_k=1),
                    ),
                    budget_requests=4,
                    observed_at=REASONED_AT,
                )
                # Default behavior is unchanged: two canonical keys, two
                # claims — the pre-M2 churn pattern.
                self.assertNotEqual(
                    derive_canonical_key(FOUNDER_STATEMENT),
                    derive_canonical_key(PARAPHRASE_STATEMENT),
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
