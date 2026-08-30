from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from veritas.providers.llm import (
    FixtureLLM,
    LLMResponse,
    OpenAICompatibleClient,
    RecordingLLM,
    fixture_key,
)


SYSTEM = "You are an extractor."
PROMPT = "Extract assertions from this document."


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _completion_payload(content: str, model: str = "deepseek-chat") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class FixtureLLMTests(unittest.TestCase):
    def test_replays_recorded_response(self) -> None:
        fixture = FixtureLLM({fixture_key(SYSTEM, PROMPT): '{"a": 1}'})
        response = fixture.complete(system=SYSTEM, prompt=PROMPT)
        self.assertEqual('{"a": 1}', response.text)
        self.assertEqual("fixture-llm", response.model_id)

    def test_unknown_prompt_raises(self) -> None:
        fixture = FixtureLLM({fixture_key(SYSTEM, PROMPT): "{}"})
        with self.assertRaises(KeyError):
            fixture.complete(system=SYSTEM, prompt="a different prompt")

    def test_round_trip_through_json(self) -> None:
        responses = {fixture_key(SYSTEM, PROMPT): '{"x": true}'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(
                json.dumps({"model_id": "fixture-llm", "responses": responses}),
                encoding="utf-8",
            )
            loaded = FixtureLLM.from_json(path)
        self.assertEqual('{"x": true}', loaded.complete(system=SYSTEM, prompt=PROMPT).text)


class RecordingLLMTests(unittest.TestCase):
    def test_records_and_replays(self) -> None:
        fixture = FixtureLLM({fixture_key(SYSTEM, PROMPT): '{"ok": 1}'})
        recorder = RecordingLLM(fixture)
        recorder.complete(system=SYSTEM, prompt=PROMPT)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recorded.json"
            recorder.save(path)
            replayed = FixtureLLM.from_json(path)
        self.assertEqual('{"ok": 1}', replayed.complete(system=SYSTEM, prompt=PROMPT).text)

    def test_accumulates_token_usage(self) -> None:
        class _CountingLLM:
            model_id = "counting-llm"

            def complete(self, *, system, prompt, json_mode=True):
                return LLMResponse(
                    text="{}", model_id=self.model_id, prompt_tokens=11, completion_tokens=7
                )

        recorder = RecordingLLM(_CountingLLM())
        recorder.complete(system=SYSTEM, prompt=PROMPT)
        recorder.complete(system=SYSTEM, prompt="another prompt")
        self.assertEqual(2, recorder.request_count)
        self.assertEqual(22, recorder.prompt_tokens)
        self.assertEqual(14, recorder.completion_tokens)


class OpenAICompatibleClientTests(unittest.TestCase):
    def _client(self, opener) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(api_key="test-key", opener=opener, max_retries=2)

    def test_request_payload_and_response_parsing(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers["Authorization"]
            return _FakeHTTPResponse(_completion_payload('{"answer": 42}'))

        response = self._client(opener).complete(system=SYSTEM, prompt=PROMPT)
        self.assertEqual('{"answer": 42}', response.text)
        self.assertEqual(11, response.prompt_tokens)
        self.assertEqual(7, response.completion_tokens)
        self.assertEqual("https://api.deepseek.com/chat/completions", captured["url"])
        self.assertEqual("Bearer test-key", captured["auth"])
        self.assertEqual(0, captured["payload"]["temperature"])
        self.assertEqual({"type": "json_object"}, captured["payload"]["response_format"])
        self.assertEqual(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT}],
            captured["payload"]["messages"],
        )

    def test_retries_on_transient_errors(self) -> None:
        calls = {"count": 0}

        def opener(request, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                raise urllib.error.URLError("connection reset")
            return _FakeHTTPResponse(_completion_payload("{}"))

        response = self._client(opener).complete(system=SYSTEM, prompt=PROMPT)
        self.assertEqual("{}", response.text)
        self.assertEqual(2, calls["count"])

    def test_client_error_is_not_retried(self) -> None:
        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 400, "bad request", {}, io.BytesIO(b"{}")
            )

        with self.assertRaises(urllib.error.HTTPError):
            self._client(opener).complete(system=SYSTEM, prompt=PROMPT)

    def test_exhausted_retries_raise(self) -> None:
        def opener(request, timeout):
            raise urllib.error.URLError("down")

        with self.assertRaises(RuntimeError):
            self._client(opener).complete(system=SYSTEM, prompt=PROMPT)

    def test_missing_api_key_raises(self) -> None:
        import os

        original = os.environ.pop("VERITAS_LLM_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                OpenAICompatibleClient()
        finally:
            if original is not None:
                os.environ["VERITAS_LLM_API_KEY"] = original

    def test_default_model_is_deepseek_v4_flash(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeHTTPResponse(_completion_payload("{}"))

        self._client(opener).complete(system=SYSTEM, prompt=PROMPT)
        self.assertEqual("deepseek-v4-flash", captured["payload"]["model"])

    def test_extra_payload_is_merged_into_request(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeHTTPResponse(_completion_payload("{}"))

        client = OpenAICompatibleClient(
            api_key="test-key",
            opener=opener,
            max_retries=2,
            extra_payload={"thinking": {"type": "disabled"}},
        )
        client.complete(system=SYSTEM, prompt=PROMPT, json_mode=False)
        self.assertEqual({"type": "disabled"}, captured["payload"]["thinking"])
        self.assertEqual(0, captured["payload"]["temperature"])
        self.assertNotIn("response_format", captured["payload"])


class LLMResponseContractTests(unittest.TestCase):
    def test_default_token_counts(self) -> None:
        response = LLMResponse(text="x", model_id="m")
        self.assertEqual(0, response.prompt_tokens)
        self.assertEqual(0, response.completion_tokens)


if __name__ == "__main__":
    unittest.main()
