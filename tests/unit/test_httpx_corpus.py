from __future__ import annotations

import unittest
from pathlib import Path

from veritas.search.local_corpus import LocalCorpusProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = PROJECT_ROOT / "datasets" / "corpus" / "httpx-docs"


class HarvestedCorpusIntegrityTests(unittest.TestCase):
    """The harvested corpus is frozen input data; these tests pin its shape."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = LocalCorpusProvider(CORPUS_ROOT)

    def test_manifest_shape(self) -> None:
        self.assertEqual("httpx-docs", self.corpus.corpus_id)
        self.assertEqual(10, len(self.corpus.document_ids()))
        for doc_id in self.corpus.document_ids():
            self.assertGreaterEqual(len(self.corpus.versions(doc_id)), 3)

    def test_retrieval_over_real_content(self) -> None:
        results = self.corpus.search("timeout configuration for requests")
        self.assertTrue(results)
        self.assertIn("quickstart", [result.doc_id for result in results[:3]])

    def test_as_of_version_selection(self) -> None:
        latest = self.corpus.latest_version("quickstart")
        self.assertEqual("0.28.1", latest)
        older = self.corpus.latest_version("quickstart", as_of="2024-06-01T00:00:00Z")
        self.assertLess(older, latest)


if __name__ == "__main__":
    unittest.main()
