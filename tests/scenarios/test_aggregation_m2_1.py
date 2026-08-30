from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.aggregation import ClaimClusterStore
from veritas.evaluation.aggregation_calibration import (
    run_calibration,
    write_summary,
)
from veritas.extraction.pipeline import claim_id_for, derive_canonical_key
from veritas.extraction.store import CandidateStore
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.runtime import ResearchRuntime, RuntimeStore, WorkItem
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
COMMITTED_SUMMARY = REPO_ROOT / "artifacts" / "aggregation" / "m2-1-calibration" / "summary.json"

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
    from veritas.extraction.pipeline import (
        EXTRACTION_SYSTEM_PROMPT,
        build_extraction_prompt,
    )

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


def _gold(case_id: str) -> str:
    benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    for case in benchmark["cases"]:
        if case["case_id"] == case_id:
            return case["expected_assertions"][0]["statement"]
    raise AssertionError(f"unknown case {case_id}")


class AggregationCalibrationM21Tests(unittest.TestCase):
    """Frozen M2-1 calibration: the deterministic clusterer against the
    real DeepSeek recording and the 32 gold assertions (D-040)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.summary = run_calibration(
            benchmark_path=BENCHMARK,
            recording_path=RECORDING,
            corpus_root=CORPUS_ROOT,
        )

    def test_frozen_calibration_matches_committed_artifact(self) -> None:
        self.assertEqual(3, self.summary["counts"]["exact_key_covered"])
        self.assertEqual(19, self.summary["counts"]["cluster_covered"])
        self.assertEqual(32, self.summary["counts"]["gold_assertions"])
        self.assertEqual(0.375, self.summary["policy"]["min_jaccard"])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "summary.json"
            write_summary(self.summary, output)
            self.assertEqual(COMMITTED_SUMMARY.read_bytes(), output.read_bytes())

    def test_boundary_true_paraphrase_merges_and_false_pair_stays_out(self) -> None:
        scores = {item["case_id"]: item["score"] for item in self.summary["matched_pairs"]}
        # EX-027: true paraphrase at 0.385 — above the frozen threshold.
        self.assertGreaterEqual(scores["EX-027"], 0.375)
        pair_27 = next(
            item for item in self.summary["matched_pairs"] if item["case_id"] == "EX-027"
        )
        self.assertIn("verify parameter", pair_27["live_statement"])
        # EX-012: a different fact at 0.364 — must not be in the merged set.
        self.assertNotIn("EX-012", scores)
        self.assertLess(0.364, self.summary["policy"]["min_jaccard"])
        # EX-029/EX-030 pin version numbers; the number guard keeps them out.
        self.assertNotIn("EX-029", scores)
        self.assertNotIn("EX-030", scores)


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
        candidates = (
            CandidateStore(tmp / "candidates.sqlite3") if cluster_store is not None else None
        )
        return (
            ResearchRuntime(
                search=search,
                provider=provider,
                store=store,
                source_namespace="fixture-corpus",
                candidate_store=candidates,
                cluster_store=cluster_store,
            ),
            store,
            candidates,
        )

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

                    founder_key = derive_canonical_key(FOUNDER_STATEMENT)
                    paraphrase_key = derive_canonical_key(PARAPHRASE_STATEMENT)
                    self.assertEqual(
                        founder_key, clusters.representative_key(paraphrase_key)
                    )
                    self.assertEqual(
                        (paraphrase_key, founder_key), clusters.find_cluster(paraphrase_key)
                    )
                    self.assertEqual({"clusters": 1, "members": 2}, clusters.counts())
                    self.assertEqual(
                        claim_id_for(founder_key),
                        claim_id_for(clusters.representative_key(paraphrase_key)),
                    )
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
                self.assertNotEqual(
                    derive_canonical_key(FOUNDER_STATEMENT),
                    derive_canonical_key(PARAPHRASE_STATEMENT),
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
