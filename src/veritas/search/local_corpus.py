from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from veritas.search.provider import SearchResult, VersionedDocument


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


class LocalCorpusProvider:
    """TF-IDF retrieval over a frozen, versioned local corpus.

    Corpus layout:

        <root>/manifest.json
        <root>/<doc_id>/<version_id>.md

    Every file's SHA-256 is pinned in the manifest and verified at load,
    so retrieval results cannot silently drift between runs.
    """

    def __init__(self, corpus_root: str | Path, *, verify_hashes: bool = True) -> None:
        self._root = Path(corpus_root)
        manifest_path = self._root / "manifest.json"
        self._manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        self._documents: dict[str, dict[str, Any]] = {}
        for document in self._manifest["documents"]:
            doc_id = document["doc_id"]
            if doc_id in self._documents:
                raise ValueError(f"duplicate document in corpus manifest: {doc_id}")
            versions = {}
            for version in document["versions"]:
                version_id = version["version_id"]
                if version_id in versions:
                    raise ValueError(f"duplicate version {version_id} for document {doc_id}")
                path = self._root / version["path"]
                if not path.is_file():
                    raise ValueError(f"corpus file missing: {path}")
                if verify_hashes:
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                    if actual != version["content_hash"]:
                        raise ValueError(
                            f"corpus hash mismatch for {doc_id}@{version_id}: "
                            f"manifest {version['content_hash'][:16]}..., file {actual[:16]}..."
                        )
                versions[version_id] = {**version, "abspath": path}
            self._documents[doc_id] = {**document, "versions": versions}

    @property
    def corpus_id(self) -> str:
        return str(self._manifest["corpus_id"])

    def document_ids(self) -> list[str]:
        return sorted(self._documents)

    def versions(self, doc_id: str) -> list[str]:
        return sorted(self._documents[doc_id]["versions"])

    def latest_version(self, doc_id: str, *, as_of: str | None = None) -> str:
        versions = self._documents[doc_id]["versions"]
        candidates = [
            version_id
            for version_id, meta in versions.items()
            if as_of is None or (meta.get("published_at") or "") <= as_of
        ]
        if not candidates:
            raise KeyError(f"no version of {doc_id} published as of {as_of}")
        return max(
            candidates,
            key=lambda version_id: (versions[version_id].get("published_at") or "", version_id),
        )

    def fetch(self, doc_id: str, version_id: str) -> VersionedDocument:
        document = self._documents[doc_id]
        meta = document["versions"][version_id]
        content = meta["abspath"].read_text(encoding="utf-8")
        return VersionedDocument(
            doc_id=doc_id,
            version_id=version_id,
            title=document["title"],
            content=content,
            published_at=meta.get("published_at"),
            content_hash=meta["content_hash"],
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        as_of: str | None = None,
    ) -> list[SearchResult]:
        query_terms = Counter(_tokens(query))
        if not query_terms:
            return []

        # Index the latest visible version of each document.
        visible: dict[str, tuple[str, list[str]]] = {}
        for doc_id in sorted(self._documents):
            try:
                version_id = self.latest_version(doc_id, as_of=as_of)
            except KeyError:
                continue
            document = self.fetch(doc_id, version_id)
            visible[doc_id] = (version_id, _tokens(document.title + "\n" + document.content))

        document_frequency: Counter[str] = Counter()
        for _, tokens in visible.values():
            for term in set(tokens):
                document_frequency[term] += 1
        corpus_size = max(len(visible), 1)

        scored: list[SearchResult] = []
        for doc_id, (version_id, tokens) in visible.items():
            term_counts = Counter(tokens)
            score = 0.0
            for term, query_count in query_terms.items():
                if term not in term_counts:
                    continue
                idf = math.log((1 + corpus_size) / (1 + document_frequency[term])) + 1.0
                score += query_count * term_counts[term] * idf
            if score <= 0:
                continue
            document = self._documents[doc_id]
            scored.append(
                SearchResult(
                    doc_id=doc_id,
                    version_id=version_id,
                    title=document["title"],
                    path=self._root / document["versions"][version_id]["path"],
                    score=score,
                    snippet=self._snippet(doc_id, version_id, set(query_terms)),
                )
            )
        scored.sort(key=lambda result: (-result.score, result.doc_id))
        return scored[:top_k]

    def _snippet(self, doc_id: str, version_id: str, terms: set[str], width: int = 40) -> str:
        tokens = self.fetch(doc_id, version_id).content.split()
        for index, token in enumerate(tokens):
            if _tokens(token) and _tokens(token)[0] in terms:
                start = max(0, index - width // 4)
                return " ".join(tokens[start : index + width])
        return " ".join(tokens[:width])
