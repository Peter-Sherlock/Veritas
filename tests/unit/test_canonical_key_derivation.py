from __future__ import annotations

import unittest

from veritas.extraction.pipeline import (
    _CANONICAL_KEY_PATTERN,
    derive_canonical_key,
)


class DeriveCanonicalKeyTests(unittest.TestCase):
    def test_key_is_lowercased_alphanumeric_tokens(self) -> None:
        self.assertEqual(
            "http_2_is_enabled_by_constructing_asyncclient_with_http2_true",
            derive_canonical_key("HTTP/2 is enabled by constructing AsyncClient with http2=True"),
        )

    def test_case_punctuation_and_whitespace_collapse_to_one_identity(self) -> None:
        variants = [
            "Cookies must be set on the client instance",
            "cookies must be set on the client instance.",
            "COOKIES MUST BE SET ON THE CLIENT, INSTANCE",
            "  cookies  must\tbe set on the client instance...  ",
        ]
        keys = {derive_canonical_key(variant) for variant in variants}
        self.assertEqual(1, len(keys))
        self.assertTrue(_CANONICAL_KEY_PATTERN.fullmatch(keys.pop()))

    def test_real_rewording_produces_a_distinct_key(self) -> None:
        self.assertNotEqual(
            derive_canonical_key("Cookies must be set on the client instance"),
            derive_canonical_key("Cookies cannot be passed per request on a Client"),
        )

    def test_key_is_stable_across_repeated_calls(self) -> None:
        statement = "HTTPX requires Python 3.7+"
        self.assertEqual(derive_canonical_key(statement), derive_canonical_key(statement))

    def test_statement_without_alphanumeric_content_derives_empty_key(self) -> None:
        self.assertEqual("", derive_canonical_key("!!! ..."))


if __name__ == "__main__":
    unittest.main()
