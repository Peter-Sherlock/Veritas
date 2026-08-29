from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.search.local_corpus import LocalCorpusProvider


def _write_doc(root: Path, doc_id: str, version: str, content: str) -> dict:
    path = root / doc_id / f"{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "version_id": version,
        "path": f"{doc_id}/{version}.md",
        "published_at": f"2026-0{version[-1]}-01T00:00:00Z",
        "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _build_corpus(root: Path) -> None:
    documents = [
        {
            "doc_id": "retry_guide",
            "title": "Retry Guide",
            "versions": [
                _write_doc(root, "retry_guide", "v1", "Atlas retries transient failures automatically."),
                _write_doc(root, "retry_guide", "v2", "Atlas retries transient failures with exponential backoff."),
            ],
        },
        {
            "doc_id": "runtime_guide",
            "title": "Runtime Guide",
            "versions": [
                _write_doc(root, "runtime_guide", "v1", "Atlas supports Python 3.11 and 3.12 runtimes."),
            ],
        },
    ]
    manifest = {"corpus_id": "test-corpus", "documents": documents}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class LocalCorpusProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        _build_corpus(self.root)
        self.corpus = LocalCorpusProvider(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_search_ranks_matching_document_first(self) -> None:
        results = self.corpus.search("exponential backoff retry")
        self.assertEqual("retry_guide", results[0].doc_id)
        self.assertEqual("v2", results[0].version_id)
        self.assertGreater(results[0].score, 0)
        self.assertIn("backoff", results[0].snippet)

    def test_search_returns_latest_visible_version(self) -> None:
        latest = self.corpus.search("transient failures")
        self.assertEqual("v2", latest[0].version_id)
        as_of_v1 = self.corpus.search("transient failures", as_of="2026-01-15T00:00:00Z")
        self.assertEqual("v1", as_of_v1[0].version_id)

    def test_search_no_match_returns_empty(self) -> None:
        self.assertEqual([], self.corpus.search("kubernetes ingress controller"))

    def test_top_k_limits_results(self) -> None:
        results = self.corpus.search("atlas", top_k=1)
        self.assertEqual(1, len(results))

    def test_fetch_returns_pinned_content(self) -> None:
        document = self.corpus.fetch("retry_guide", "v1")
        self.assertIn("automatically", document.content)
        self.assertEqual("Retry Guide", document.title)
        self.assertEqual(64, len(document.content_hash))

    def test_hash_tampering_is_rejected_at_load(self) -> None:
        target = self.root / "retry_guide" / "v2.md"
        target.write_text("tampered content", encoding="utf-8")
        with self.assertRaises(ValueError):
            LocalCorpusProvider(self.root)

    def test_duplicate_document_id_is_rejected(self) -> None:
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["documents"].append(manifest["documents"][0])
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            LocalCorpusProvider(self.root)

    def test_versions_listing(self) -> None:
        self.assertEqual(["v1", "v2"], self.corpus.versions("retry_guide"))
        self.assertEqual("test-corpus", self.corpus.corpus_id)
        self.assertEqual(["retry_guide", "runtime_guide"], self.corpus.document_ids())


if __name__ == "__main__":
    unittest.main()
