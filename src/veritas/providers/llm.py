from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(Protocol):
    """Minimal structured-completion boundary used by planning/extraction."""

    @property
    def model_id(self) -> str: ...

    def complete(self, *, system: str, prompt: str, json_mode: bool = True) -> LLMResponse: ...


def fixture_key(system: str, prompt: str) -> str:
    """Stable key identifying one (system, prompt) exchange for fixtures."""
    return hashlib.sha256(f"{system}\n\n{prompt}".encode("utf-8")).hexdigest()


class FixtureLLM:
    """Deterministic provider replaying pre-recorded responses.

    Responses are keyed by fixture_key(system, prompt). Any unknown prompt
    raises immediately so tests cannot silently pass against a live model.
    """

    def __init__(self, responses: Mapping[str, str], *, model_id: str = "fixture-llm") -> None:
        self._responses = dict(responses)
        self._model_id = model_id

    @classmethod
    def from_json(cls, path: str | Path) -> "FixtureLLM":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["responses"], model_id=data.get("model_id", "fixture-llm"))

    @property
    def model_id(self) -> str:
        return self._model_id

    def complete(self, *, system: str, prompt: str, json_mode: bool = True) -> LLMResponse:
        key = fixture_key(system, prompt)
        if key not in self._responses:
            raise KeyError(
                "no recorded response for this prompt; "
                f"fixture has {len(self._responses)} entries, missing key {key[:16]}..."
            )
        return LLMResponse(text=self._responses[key], model_id=self._model_id)


class RecordingLLM:
    """Wraps a live provider and records exchanges for later replay.

    Token usage is accumulated across complete() calls so a recording run can
    report its own cost; it is not part of the saved fixture payload.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self._responses: dict[str, str] = {}
        self.request_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    def complete(self, *, system: str, prompt: str, json_mode: bool = True) -> LLMResponse:
        response = self._inner.complete(system=system, prompt=prompt, json_mode=json_mode)
        self._responses[fixture_key(system, prompt)] = response.text
        self.request_count += 1
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        return response

    def save(self, path: str | Path) -> None:
        payload = {"model_id": self._inner.model_id, "responses": self._responses}
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


OpenerType = Callable[[urllib.request.Request, float], Any]


class OpenAICompatibleClient:
    """Stdlib-only client for OpenAI-compatible chat completion APIs
    (DeepSeek, Kimi, Qwen, ...). Zero third-party dependencies.

    The API key comes from the constructor or VERITAS_LLM_API_KEY.
    ``extra_payload`` is merged into the request body after the standard
    fields, so provider-specific parameters (e.g. DeepSeek's
    ``thinking`` mode switch) stay out of the client itself.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        extra_payload: Mapping[str, Any] | None = None,
        opener: OpenerType | None = None,
    ) -> None:
        key = api_key or os.environ.get("VERITAS_LLM_API_KEY")
        if not key:
            raise ValueError("an API key is required (argument or VERITAS_LLM_API_KEY)")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = key
        self._timeout = timeout
        self._max_retries = max_retries
        self._extra_payload = dict(extra_payload) if extra_payload else {}
        self._opener: OpenerType = opener or (
            lambda request, timeout: urllib.request.urlopen(request, timeout=timeout)
        )

    @property
    def model_id(self) -> str:
        return self._model

    def complete(self, *, system: str, prompt: str, json_mode: bool = True) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self._extra_payload)
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        body = self._send_with_retry(request)
        data = json.loads(body)
        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return LLMResponse(
            text=choice,
            model_id=str(data.get("model") or self._model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _send_with_retry(self, request: urllib.request.Request) -> str:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                with self._opener(request, self._timeout) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code in (408, 409, 429) or exc.code >= 500
                exc.close()
                if not retryable:
                    raise
            except urllib.error.URLError as exc:
                last_error = exc
            except http.client.HTTPException as exc:
                # Transport-level truncation (IncompleteRead, BadStatusLine,
                # dropped connections): transient, retry the whole request.
                last_error = exc
            time.sleep(min(2**attempt, 8))
        raise RuntimeError(
            f"LLM request failed after {self._max_retries} attempts: {last_error}"
        ) from last_error
