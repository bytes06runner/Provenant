"""HTTP adapters: OpenAI-compatible (Groq) and Gemini. One call in, one LLMResponse out.

Adapters translate requests and responses and classify errors. Retries, fallback, budgets and
caching live in the router, so every provider gets the same policy.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from llm.types import LLMError, LLMRequest, LLMResponse, RateLimited, Usage


class Provider(Protocol):
    name: str

    def complete(self, model: str, request: LLMRequest) -> LLMResponse: ...


def _seconds(text: str | None) -> float | None:
    """Parse '30', '30s', '1.5s', '1m26.4s' style durations."""
    if not text:
        return None
    m = re.fullmatch(r"(?:(\d+)m)?(\d+(?:\.\d+)?)s?", text.strip())
    if not m:
        return None
    return float(m.group(1) or 0) * 60 + float(m.group(2))


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        *,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._clock = clock
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def _messages(self, request: LLMRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in request.messages:
            if not m.images:
                out.append({"role": m.role, "content": m.content})
                continue
            parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
            parts += [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{i.mime_type};base64,{i.b64()}"},
                }
                for i in m.images
            ]
            out.append({"role": m.role, "content": parts})
        return out

    def complete(self, model: str, request: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": self._messages(request),
            "temperature": float(request.temperature),
            "max_completion_tokens": request.max_tokens,
            **request.options,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
        started = self._clock()
        try:
            resp = self._http.post("/chat/completions", json=body)
        except httpx.TransportError as e:
            raise LLMError(self.name, model, None, f"transport: {e}") from e
        latency = int((self._clock() - started) * 1000)
        if resp.status_code == 429:
            raise RateLimited(
                self.name,
                model,
                _error_message(resp),
                _seconds(resp.headers.get("retry-after")),
            )
        if resp.status_code >= 400:
            raise LLMError(self.name, model, resp.status_code, _error_message(resp))
        data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return LLMResponse(
            provider=self.name,
            model=model,
            text=choice["message"].get("content") or "",
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                reasoning_tokens=int(details.get("reasoning_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0)),
            ),
            finish_reason=choice.get("finish_reason"),
            model_version=data.get("system_fingerprint"),
            response_id=data.get("id"),
            latency_ms=latency,
        )


class GeminiProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        *,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._clock = clock
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"x-goog-api-key": api_key},
        )

    def complete(self, model: str, request: LLMRequest) -> LLMResponse:
        system = [m.content for m in request.messages if m.role == "system"]
        contents = []
        for m in request.messages:
            if m.role == "system":
                continue
            parts: list[dict[str, Any]] = [{"text": m.content}]
            parts += [{"inlineData": {"mimeType": i.mime_type, "data": i.b64()}} for i in m.images]
            contents.append({"role": "model" if m.role == "assistant" else "user", "parts": parts})
        config: dict[str, Any] = {
            "temperature": float(request.temperature),
            "maxOutputTokens": request.max_tokens,
            **request.options,
        }
        if request.seed is not None:
            config["seed"] = request.seed
        if request.json_schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseJsonSchema"] = request.json_schema
        body: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        started = self._clock()
        try:
            resp = self._http.post(f"/models/{model}:generateContent", json=body)
        except httpx.TransportError as e:
            raise LLMError(self.name, model, None, f"transport: {e}") from e
        latency = int((self._clock() - started) * 1000)
        if resp.status_code == 429:
            raise RateLimited(self.name, model, _error_message(resp), _gemini_retry(resp))
        if resp.status_code >= 400:
            raise LLMError(self.name, model, resp.status_code, _error_message(resp))
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            block = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(self.name, model, resp.status_code, f"empty response: {block}")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        meta = data.get("usageMetadata") or {}
        return LLMResponse(
            provider=self.name,
            model=model,
            text=text,
            usage=Usage(
                input_tokens=int(meta.get("promptTokenCount", 0)),
                output_tokens=int(meta.get("candidatesTokenCount", 0)),
                reasoning_tokens=int(meta.get("thoughtsTokenCount", 0)),
                total_tokens=int(meta.get("totalTokenCount", 0)),
            ),
            finish_reason=cands[0].get("finishReason"),
            model_version=data.get("modelVersion"),
            response_id=data.get("responseId"),
            latency_ms=latency,
        )


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    err = data.get("error", data)
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err)[:300]


def _gemini_retry(resp: httpx.Response) -> float | None:
    try:
        details = resp.json()["error"].get("details", [])
    except (ValueError, KeyError, AttributeError):
        return None
    for d in details:
        if str(d.get("@type", "")).endswith("RetryInfo"):
            return _seconds(d.get("retryDelay"))
    return None
