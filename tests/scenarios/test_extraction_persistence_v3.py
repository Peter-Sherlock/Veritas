from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from veritas.evaluation.extraction_runner import (
    build_fixture_provider,
    evaluate_extraction_calibration,
)
from veritas.extraction.pipeline import derive_canonical_key
from veritas.extraction.store import (
    CandidateStore,
    candidate_content_hash,
    candidates_from_document,
)
from veritas.providers.llm import FixtureLLM
from veritas.search.local_corpus import LocalCorpusProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2c"
BENCHMARK = DATASET / "benchmark.json"
FIXTURES = DATASET / "fixtures.json"
LIVE_DIR = REPO_ROOT / "artifacts" / "extraction" / "httpx-initial-extraction-3.0.0-deepseek-v4-flash"
RECORDING = LIVE_DIR / "responses-recording.json"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"

MODEL_ID = "deepseek-v4-flash"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _persisting_callback(
    store: CandidateStore,
    corpus: LocalCorpusProvider,
    run_id: str,
    observed_at: str,
    batch_stats: list[dict[str, int]],
):
    def on_case_done(case_result: dict[str, Any], bundle: Any) -> None:
        if bundle is None:
            return
        records = [
            record
            for document in bundle.documents
            for record in candidates_from_document(document, source_namespace=corpus.corpus_id)
        ]
        batch_stats.append(
            store.persist(records, run_id=run_id, observed_at=observed_at)
        )

    return on_case_done


def _gold_identity_union(
    benchmark: dict[str, Any],
    fixtures: dict[str, Any],
    corpus: LocalCorpusProvider,
) -> set[tuple[str, str, str]]:
    """The candidate identities the frozen gold assertions must produce."""
    fixture_cases = {case["case_id"]: case for case in fixtures["cases"]}
    identities: set[tuple[str, str, str]] = set()
    for case in benchmark["cases"]:
        versions = fixture_cases[case["case_id"]]["versions"]
        for item in case["expected_assertions"]:
            source_version_id = (
                f"{corpus.corpus_id}:{item['doc_id']}@{versions[item['doc_id']]}"
            )
            identities.add(
                (
                    source_version_id,
                    derive_canonical_key(item["statement"]),
                    candidate_content_hash(
                        statement=item["statement"],
                        relation=item["relation"],
                        quote=item["quote"],
                    ),
                )
            )
    return identities


class ExtractionPersistenceV3Tests(unittest.TestCase):
    def test_fixture_replay_persists_the_gold_union_idempotently(self) -> None:
        benchmark = _load(BENCHMARK)
        fixtures = _load(FIXTURES)
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        expected_union = _gold_identity_union(benchmark, fixtures, corpus)
        self.assertEqual(32, len(expected_union))

        run_id = f"fixture:{fixtures['fixture_id']}"
        with tempfile.TemporaryDirectory() as tmp:
            store = CandidateStore(Path(tmp) / "fixture-candidates.db")
            try:
                batch_stats: list[dict[str, int]] = []
                summary = evaluate_extraction_calibration(
                    benchmark=benchmark,
                    fixtures=fixtures,
                    corpus=corpus,
                    provider=build_fixture_provider(benchmark, fixtures, corpus),
                    on_case_done=_persisting_callback(
                        store, corpus, run_id, benchmark["reasoned_at"], batch_stats
                    ),
                )
                self.assertEqual(30, summary["passed_case_count"])
                self.assertEqual(
                    {
                        "candidates": len(expected_union),
                        "observations": len(expected_union),
                        "distinct_canonical_keys": len(expected_union),
                    },
                    store.counts(),
                )
                self.assertEqual([], store.list_relation_conflicts())

                # A second deterministic replay must not add anything.
                rerun_stats: list[dict[str, int]] = []
                evaluate_extraction_calibration(
                    benchmark=benchmark,
                    fixtures=fixtures,
                    corpus=corpus,
                    provider=build_fixture_provider(benchmark, fixtures, corpus),
                    on_case_done=_persisting_callback(
                        store, corpus, run_id, benchmark["reasoned_at"], rerun_stats
                    ),
                )
                self.assertEqual(
                    [0, 0],
                    [
                        sum(stats["persisted"] for stats in rerun_stats),
                        sum(stats["observations_new"] for stats in rerun_stats),
                    ],
                )
                self.assertEqual(
                    {
                        "candidates": len(expected_union),
                        "observations": len(expected_union),
                        "distinct_canonical_keys": len(expected_union),
                    },
                    store.counts(),
                )
            finally:
                store.close()

    def test_live_recording_persists_candidates_and_pins_paraphrase_noise(self) -> None:
        benchmark = _load(BENCHMARK)
        recording = _load(RECORDING)
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        provider = FixtureLLM(recording["responses"], model_id=recording["model_id"])

        with tempfile.TemporaryDirectory() as tmp:
            store = CandidateStore(Path(tmp) / "live-candidates.db")
            try:
                batch_stats: list[dict[str, int]] = []
                evaluate_extraction_calibration(
                    benchmark=benchmark,
                    fixtures={
                        "fixture_id": f"live-recording:{MODEL_ID}",
                        "model_id": MODEL_ID,
                    },
                    corpus=corpus,
                    provider=provider,
                    on_case_done=_persisting_callback(
                        store,
                        corpus,
                        f"live:{MODEL_ID}",
                        benchmark["reasoned_at"],
                        batch_stats,
                    ),
                )
                # The live run's 26 contract-passing cases produced 52
                # candidates; none repeated within the run.
                self.assertEqual(
                    {"candidates": 52, "observations": 52, "distinct_canonical_keys": 51},
                    store.counts(),
                )
                self.assertEqual([], store.list_relation_conflicts())

                # Paraphrase noise, pinned: quickstart@0.28.1 holds 15
                # candidates with 15 distinct keys, and neither EX-014 gold
                # key is among them — the model reworded the fact into new
                # claim identities instead of reproducing the gold statement.
                rows = store.connection.execute(
                    """
                    SELECT canonical_key FROM extraction_candidates
                    WHERE source_version_id = ?
                    """,
                    (f"{corpus.corpus_id}:quickstart@0.28.1",),
                ).fetchall()
                self.assertEqual(15, len(rows))
                self.assertEqual(15, len({row["canonical_key"] for row in rows}))
                gold_keys = {
                    derive_canonical_key(item["statement"])
                    for case in benchmark["cases"]
                    if case["case_id"] == "EX-014"
                    for item in case["expected_assertions"]
                }
                self.assertEqual(2, len(gold_keys))
                self.assertEqual(set(), gold_keys & {row["canonical_key"] for row in rows})
            finally:
                store.close()

    def test_fixture_and_live_runs_share_one_store_without_merging(self) -> None:
        benchmark = _load(BENCHMARK)
        fixtures = _load(FIXTURES)
        recording = _load(RECORDING)
        corpus = LocalCorpusProvider(CORPUS_ROOT)

        with tempfile.TemporaryDirectory() as tmp:
            store = CandidateStore(Path(tmp) / "shared-candidates.db")
            try:
                fixture_stats: list[dict[str, int]] = []
                evaluate_extraction_calibration(
                    benchmark=benchmark,
                    fixtures=fixtures,
                    corpus=corpus,
                    provider=build_fixture_provider(benchmark, fixtures, corpus),
                    on_case_done=_persisting_callback(
                        store,
                        corpus,
                        f"fixture:{fixtures['fixture_id']}",
                        benchmark["reasoned_at"],
                        fixture_stats,
                    ),
                )
                live_stats: list[dict[str, int]] = []
                evaluate_extraction_calibration(
                    benchmark=benchmark,
                    fixtures={
                        "fixture_id": f"live-recording:{MODEL_ID}",
                        "model_id": MODEL_ID,
                    },
                    corpus=corpus,
                    provider=FixtureLLM(
                        recording["responses"], model_id=recording["model_id"]
                    ),
                    on_case_done=_persisting_callback(
                        store,
                        corpus,
                        f"live:{MODEL_ID}",
                        benchmark["reasoned_at"],
                        live_stats,
                    ),
                )
                # The live run deduplicated against nothing: not one of its 52
                # candidates equals a gold candidate, matching the 0/30 exact
                # match of the frozen live summary.
                self.assertEqual(52, sum(stats["persisted"] for stats in live_stats))
                self.assertEqual(0, sum(stats["deduped"] for stats in live_stats))
                self.assertEqual(
                    {
                        "candidates": 84,
                        "observations": 84,
                        "distinct_canonical_keys": 80,
                    },
                    store.counts(),
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
