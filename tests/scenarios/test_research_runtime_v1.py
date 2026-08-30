from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from veritas.evaluation.extraction_runner import build_fixture_provider
from veritas.extraction.pipeline import derive_canonical_key
from veritas.runtime import (
    ResearchRuntime,
    RuntimeSessionError,
    RuntimeStore,
    WorkItem,
)
from veritas.search.local_corpus import LocalCorpusProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "datasets" / "extraction" / "httpx-m1-2c"
BENCHMARK = DATASET / "benchmark.json"
FIXTURES = DATASET / "fixtures.json"
CORPUS_ROOT = REPO_ROOT / "datasets" / "corpus" / "httpx-docs"

SESSION_ID = "scenario-m1-3a"
CASE_IDS = ("EX-001", "EX-002", "EX-003")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ResearchRuntimeScenarioTests(unittest.TestCase):
    def test_frozen_fixture_session_completes_and_replays_idempotently(self) -> None:
        benchmark = _load(BENCHMARK)
        fixtures = _load(FIXTURES)
        corpus = LocalCorpusProvider(CORPUS_ROOT)
        cases = {
            case["case_id"]: case
            for case in benchmark["cases"]
            if case["case_id"] in CASE_IDS
        }
        items = [
            WorkItem(
                item_id=case_id,
                query=cases[case_id]["query"],
                question=cases[case_id]["question"],
                top_k=int(cases[case_id]["top_k"]),
                as_of=cases[case_id].get("as_of"),
            )
            for case_id in CASE_IDS
        ]
        gold_keys = {
            derive_canonical_key(item["statement"])
            for case_id in CASE_IDS
            for item in cases[case_id]["expected_assertions"]
        }
        self.assertEqual(3, len(gold_keys))

        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeStore(Path(tmp) / "runtime.db") as store:
                from veritas.extraction.store import CandidateStore

                with CandidateStore(Path(tmp) / "candidates.db") as candidates:
                    runtime = ResearchRuntime(
                        search=corpus,
                        provider=build_fixture_provider(benchmark, fixtures, corpus),
                        store=store,
                        source_namespace=corpus.corpus_id,
                        candidate_store=candidates,
                    )
                    result = runtime.run(
                        session_id=SESSION_ID,
                        items=items,
                        budget_requests=20,
                        observed_at=benchmark["reasoned_at"],
                    )
                    self.assertEqual("completed", result["status"])
                    self.assertEqual(3, result["items_completed"])
                    # Three items at top_k 3: nine reserved requests.
                    self.assertEqual(9, result["requests_spent"])
                    # The fixture provider reproduces the gold assertions, so
                    # the session's candidates are exactly the three gold
                    # identities, each observed once under the session run id.
                    self.assertEqual(
                        {"candidates": 3, "observations": 3, "distinct_canonical_keys": 3},
                        candidates.counts(),
                    )
                    stored_keys = {
                        row["canonical_key"]
                        for row in candidates.connection.execute(
                            "SELECT canonical_key FROM extraction_candidates"
                        )
                    }
                    self.assertEqual(gold_keys, stored_keys)

                    # A completed session is closed: replaying it errors out
                    # and leaves store and candidates untouched.
                    with self.assertRaises(RuntimeSessionError) as caught:
                        runtime.run(
                            session_id=SESSION_ID,
                            items=items,
                            budget_requests=20,
                            observed_at=benchmark["reasoned_at"],
                        )
                    self.assertEqual("session_completed", caught.exception.code)
                    self.assertEqual(9, store.session_state(SESSION_ID)["requests_spent"])
                    self.assertEqual(
                        {"candidates": 3, "observations": 3},
                        {
                            "candidates": candidates.counts()["candidates"],
                            "observations": candidates.counts()["observations"],
                        },
                    )


if __name__ == "__main__":
    unittest.main()
