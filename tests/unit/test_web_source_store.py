"""Unit invariants for the versioned web-source adapter (M4-1, D-048).

Covers the canonical content contract, slug safety, the append-only
ledger identity (new version / SAME observation / revert / replay /
conflict), typed fetch-shell failures, schema drift and the byte
determinism of materialization. The real network path is exercised
once against a localhost HTTP server; everything else runs on injected
transports.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from veritas.search.local_corpus import LocalCorpusProvider
from veritas.sources import (
    WebSourceError,
    WebSourceStore,
    canonical_web_text,
    fetch_web_source,
    materialize_corpus,
    url_slug,
)


URL = "https://example.com/status/retries"
V1 = "The service retries failed requests up to 3 times.\n"
V2 = "The service retries failed requests up to 5 times.\n"
T1 = "2026-09-02T10:00:00Z"
T2 = "2026-09-02T11:00:00Z"
T3 = "2026-09-02T12:00:00Z"


class FakeTransport:
    """Scripted transport: url -> (status, body), mutable for revisions."""

    def __init__(self, pages: dict[str, tuple[int, bytes]]) -> None:
        self.pages = dict(pages)

    def __call__(self, url: str) -> tuple[int, bytes]:
        if url not in self.pages:
            raise AssertionError(f"unexpected fetch of {url!r}")
        return self.pages[url]


class CanonicalTextTests(unittest.TestCase):
    def test_crlf_and_cr_fold_to_lf(self) -> None:
        self.assertEqual(canonical_web_text(b"a\r\nb\rc\nd"), "a\nb\nc\nd")

    def test_non_utf8_body_is_typed_rejection(self) -> None:
        with self.assertRaises(WebSourceError) as caught:
            canonical_web_text(b"\xff\xfe\x00")
        self.assertEqual("non_utf8_body", caught.exception.code)

    def test_whitespace_only_body_is_rejected(self) -> None:
        with self.assertRaises(WebSourceError) as caught:
            canonical_web_text(b"  \r\n\t")
        self.assertEqual("empty_web_body", caught.exception.code)


class SlugTests(unittest.TestCase):
    def test_slug_is_deterministic_and_filesystem_safe(self) -> None:
        slug = url_slug(URL)
        again = url_slug(URL)
        self.assertEqual(slug, again)
        self.assertTrue(slug.startswith("example.com-status-retries-"))
        self.assertTrue(all(c.isalnum() or c in "._-" for c in slug))
        self.assertNotIn(":", slug)

    def test_distinct_urls_get_distinct_slugs(self) -> None:
        self.assertNotEqual(url_slug(URL), url_slug("https://example.com/status/uptime"))

    def test_non_http_url_is_rejected(self) -> None:
        for bad in ("", "ftp://example.com/x", "example.com/x"):
            with self.assertRaises(WebSourceError) as caught:
                url_slug(bad)
            self.assertEqual("invalid_web_url", caught.exception.code)


class LedgerTests(unittest.TestCase):
    def test_fetch_timeline_same_content_and_revert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            try:
                first = store.record_fetch(URL, V1, fetched_at=T1)
                self.assertEqual("new_version", first.status)
                self.assertEqual("f1", first.version_label)

                same = store.record_fetch(URL, V1, fetched_at=T2)
                self.assertEqual("same_content", same.status)
                self.assertEqual("f1", same.version_label)

                change = store.record_fetch(URL, V2, fetched_at=T3)
                self.assertEqual("new_version", change.status)
                self.assertEqual("f2", change.version_label)

                revert = store.record_fetch(URL, V1, fetched_at="2026-09-02T13:00:00Z")
                self.assertEqual("new_version", revert.status)
                self.assertEqual("f3", revert.version_label)

                self.assertEqual(
                    ["f1", "f2", "f3"],
                    [v.version_label for v in store.versions(URL)],
                )
                self.assertEqual(V1, store.latest(URL).content)
                self.assertEqual("2026-09-02T13:00:00Z", store.latest(URL).published_at)
                self.assertEqual(
                    {"urls": 1, "fetches": 4, "versions": 3}, store.counts()
                )
            finally:
                store.close()

    def test_replay_of_recorded_fetch_reproduces_the_stored_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            try:
                original = store.record_fetch(URL, V1, fetched_at=T1)
                replay = store.record_fetch(URL, V1, fetched_at=T1)
                self.assertEqual(original, replay)

                store.record_fetch(URL, V1, fetched_at=T2)  # SAME observation
                store.record_fetch(URL, V2, fetched_at=T3)
                store.record_fetch(URL, V1, fetched_at="2026-09-02T13:00:00Z")  # revert
                replay_of_same = store.record_fetch(URL, V1, fetched_at=T2)
                # The T2 fetch observed V1 while f1 was current: SAME.
                self.assertEqual("same_content", replay_of_same.status)
                self.assertEqual("f1", replay_of_same.version_label)
            finally:
                store.close()

    def test_conflicting_payload_at_one_instant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            try:
                store.record_fetch(URL, V1, fetched_at=T1)
                with self.assertRaises(WebSourceError) as caught:
                    store.record_fetch(URL, V2, fetched_at=T1)
                self.assertEqual("web_fetch_conflict", caught.exception.code)
                self.assertEqual(1, store.counts()["fetches"])
            finally:
                store.close()

    def test_empty_content_and_bad_urls_are_rejected_before_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            try:
                with self.assertRaises(WebSourceError) as caught:
                    store.record_fetch(URL, "   ", fetched_at=T1)
                self.assertEqual("empty_web_body", caught.exception.code)
                with self.assertRaises(WebSourceError) as caught:
                    store.record_fetch("gopher://example.com", V1, fetched_at=T1)
                self.assertEqual("invalid_web_url", caught.exception.code)
                self.assertEqual(0, store.counts()["fetches"])
            finally:
                store.close()

    def test_schema_drift_is_rejected_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.sqlite3"
            store = WebSourceStore(path)
            store.record_fetch(URL, V1, fetched_at=T1)
            store.close()
            raw = sqlite3.connect(path)
            try:
                raw.execute(
                    "UPDATE store_meta SET value = 'web-sources-0' WHERE key = 'schema'"
                )
                raw.commit()
            finally:
                raw.close()
            with self.assertRaises(WebSourceError) as caught:
                WebSourceStore(path)
            self.assertEqual("web_store_schema_drift", caught.exception.code)


class FetchShellTests(unittest.TestCase):
    def test_http_error_is_typed_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            transport = FakeTransport({URL: (404, b"gone")})
            try:
                with self.assertRaises(WebSourceError) as caught:
                    fetch_web_source(
                        store, URL, observed_at=T1, transport=transport
                    )
                self.assertEqual("web_fetch_http_error", caught.exception.code)
                self.assertEqual(0, store.counts()["fetches"])
            finally:
                store.close()

    def test_transport_failure_is_typed_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")

            def broken(url: str) -> tuple[int, bytes]:
                raise TimeoutError("connection timed out")

            try:
                with self.assertRaises(WebSourceError) as caught:
                    fetch_web_source(store, URL, observed_at=T1, transport=broken)
                self.assertEqual("web_fetch_unreachable", caught.exception.code)
                self.assertEqual(0, store.counts()["fetches"])
            finally:
                store.close()

    def test_oversized_body_is_rejected_before_canonicalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            transport = FakeTransport({URL: (200, b"a" * 64)})
            try:
                with self.assertRaises(WebSourceError) as caught:
                    fetch_web_source(
                        store, URL, observed_at=T1, transport=transport, max_bytes=8
                    )
                self.assertEqual("web_body_too_large", caught.exception.code)
                self.assertEqual(0, store.counts()["fetches"])
            finally:
                store.close()

    def test_non_utf8_response_is_rejected_before_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            transport = FakeTransport({URL: (200, b"\xff\xfe")})
            try:
                with self.assertRaises(WebSourceError) as caught:
                    fetch_web_source(
                        store, URL, observed_at=T1, transport=transport
                    )
                self.assertEqual("non_utf8_body", caught.exception.code)
                self.assertEqual(0, store.counts()["fetches"])
            finally:
                store.close()

    def test_real_localhost_http_round_trip_and_404(self) -> None:
        bodies = {"/retries": V1.encode("utf-8")}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                body = bodies.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            store = WebSourceStore(Path(tmp) / "web.sqlite3")
            try:
                live_url = f"http://127.0.0.1:{server.server_port}/retries"
                outcome = fetch_web_source(store, live_url, observed_at=T1)
                self.assertEqual("new_version", outcome.status)
                self.assertEqual(
                    hashlib.sha256(V1.encode("utf-8")).hexdigest(),
                    outcome.content_hash,
                )
                self.assertEqual(V1, store.latest(live_url).content)
                with self.assertRaises(WebSourceError) as caught:
                    fetch_web_source(
                        store, f"http://127.0.0.1:{server.server_port}/missing", observed_at=T1
                    )
                self.assertEqual("web_fetch_http_error", caught.exception.code)
            finally:
                store.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)


class MaterializeTests(unittest.TestCase):
    def _build_store(self, path: Path) -> WebSourceStore:
        store = WebSourceStore(path)
        other = "https://example.com/status/uptime"
        store.record_fetch(URL, V1, fetched_at=T1)
        store.record_fetch(URL, V1, fetched_at=T2)  # SAME observation
        store.record_fetch(URL, V2, fetched_at=T3)
        store.record_fetch(URL, V1, fetched_at="2026-09-02T13:00:00Z")  # revert
        store.record_fetch(other, "The uptime endpoint reports 99.9%.\n", fetched_at=T1)
        return store

    def test_materialized_corpus_loads_with_hash_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._build_store(root / "web.sqlite3")
            try:
                corpus_root = materialize_corpus(store, root / "corpus", corpus_id="webwatch")
                provider = LocalCorpusProvider(corpus_root)
                self.assertEqual("webwatch", provider.corpus_id)
                slug = url_slug(URL)
                self.assertEqual([slug, url_slug("https://example.com/status/uptime")], provider.document_ids())
                self.assertEqual(["f1", "f2", "f3"], provider.versions(slug))
                self.assertEqual(V2, provider.fetch(slug, "f2").content)
                self.assertEqual(V1, provider.fetch(slug, "f3").content)
            finally:
                store.close()

    def test_rematerialization_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._build_store(root / "web.sqlite3")
            try:
                first = materialize_corpus(store, root / "corpus-a", corpus_id="webwatch")
                second = materialize_corpus(store, root / "corpus-b", corpus_id="webwatch")
                first_files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
                second_files = sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
                self.assertEqual(first_files, second_files)
                for relative in first_files:
                    self.assertEqual(
                        (first / relative).read_bytes(),
                        (second / relative).read_bytes(),
                        f"materialization drift in {relative}",
                    )
                manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual("webwatch", manifest["corpus_id"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
