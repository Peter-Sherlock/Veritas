"""One-off corpus harvesting tool (NOT part of the runtime).

Extracts versioned documentation snapshots from an open-source project's
git history into the frozen Veritas corpus layout:

    datasets/corpus/<corpus_id>/manifest.json
    datasets/corpus/<corpus_id>/<doc_id>/<version_id>.md

Each harvested file's SHA-256 is pinned in the manifest. The corpus is a
build artifact: review it, commit it, and treat it as frozen input data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_REPO = "https://github.com/encode/httpx.git"
DEFAULT_TAGS = ["0.24.1", "0.25.2", "0.26.0", "0.27.2", "0.28.1"]
DEFAULT_DOCS = [
    "docs/quickstart.md",
    "docs/advanced.md",
    "docs/api.md",
    "docs/http2.md",
    "docs/environment_variables.md",
    "docs/troubleshooting.md",
    "docs/async.md",
    "docs/compatibility.md",
    "docs/index.md",
    "docs/contributing.md",
]


def _canonical_text(text: str) -> str:
    """Use LF as the corpus byte contract on every operating system."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def _doc_id(path: str) -> str:
    stem = Path(path).stem
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")


def harvest(repo: str, corpus_id: str, tags: list[str], docs: list[str], out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    manifest_documents: dict[str, dict] = {}
    harvested = 0

    with tempfile.TemporaryDirectory(prefix="veritas-harvest-") as tmp:
        repo_dir = Path(tmp) / "repo"
        print(f"cloning {repo} (blobless) ...")
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(repo_dir)],
            check=True,
        )

        for tag in tags:
            try:
                published_at = _git(repo_dir, "log", "-1", "--format=%cI", tag).strip()
            except subprocess.CalledProcessError:
                print(f"  ! tag not found, skipped: {tag}")
                continue
            for doc_path in docs:
                try:
                    content = _canonical_text(_git(repo_dir, "show", f"{tag}:{doc_path}"))
                except subprocess.CalledProcessError:
                    continue
                doc_id = _doc_id(doc_path)
                title = doc_id.replace("_", " ")
                version_id = tag
                target = out / doc_id / f"{version_id}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                canonical_bytes = content.encode("utf-8")
                target.write_bytes(canonical_bytes)
                digest = hashlib.sha256(canonical_bytes).hexdigest()
                entry = manifest_documents.setdefault(
                    doc_id, {"doc_id": doc_id, "title": title, "versions": []}
                )
                entry["versions"].append(
                    {
                        "version_id": version_id,
                        "path": f"{doc_id}/{version_id}.md",
                        "published_at": published_at,
                        "content_hash": digest,
                        "source_ref": f"{repo}@{tag}:{doc_path}",
                    }
                )
                harvested += 1

    documents = []
    for doc_id in sorted(manifest_documents):
        entry = manifest_documents[doc_id]
        entry["versions"].sort(key=lambda v: (v["published_at"], v["version_id"]))
        documents.append(entry)

    manifest = {"corpus_id": corpus_id, "documents": documents}
    (out / "manifest.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(f"harvested {harvested} versioned documents into {out}")
    print(f"documents: {len(documents)}")
    for document in documents:
        print(f"  {document['doc_id']}: {len(document['versions'])} versions")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Harvest a versioned doc corpus from git history")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--corpus-id", default="httpx-docs")
    parser.add_argument("--tags", nargs="*", default=DEFAULT_TAGS)
    parser.add_argument("--docs", nargs="*", default=DEFAULT_DOCS)
    parser.add_argument("--out", type=Path, default=Path("datasets/corpus/httpx-docs"))
    args = parser.parse_args()
    return harvest(args.repo, args.corpus_id, args.tags, args.docs, args.out)


if __name__ == "__main__":
    sys.exit(main())
