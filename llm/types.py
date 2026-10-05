"""Provider-neutral request and response types for the LLM layer."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Image:
    data: bytes
    mime_type: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    images: tuple[Image, ...] = ()


@dataclass(frozen=True)
class LLMRequest:
    """One model call. `json_schema` asks for structured output that must match the schema.

    `temperature` is a decimal string ("0", "0.7"), converted to a number only at the HTTP
    boundary, so request identity (cache keys, recorder payloads) never contains a float.
    """

    messages: tuple[Message, ...]
    temperature: str = "0"
    max_tokens: int = 1024
    seed: int | None = None
    json_schema: dict[str, Any] | None = None
    schema_name: str = "output"
    options: dict[str, Any] = field(default_factory=dict)  # provider-specific passthrough

    def identity(self) -> dict[str, Any]:
        """Everything that determines the output, in JSON-safe form (for cache keys)."""
        return {
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "images": [i.sha256 for i in m.images],
                }
                for m in self.messages
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "json_schema": self.json_schema,
            "options": self.options,
        }

    def text_chars(self) -> int:
        return sum(len(m.content) for m in self.messages)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class LLMResponse:
    provider: str
    model: str
    text: str
    usage: Usage
    finish_reason: str | None = None
    model_version: str | None = None  # system_fingerprint (Groq) or modelVersion (Gemini)
    response_id: str | None = None
    latency_ms: int = 0
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "text": self.text,
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
            "model_version": self.model_version,
            "response_id": self.response_id,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, cached: bool = False) -> LLMResponse:
        return cls(
            provider=d["provider"],
            model=d["model"],
            text=d["text"],
            usage=Usage(**d["usage"]),
            finish_reason=d.get("finish_reason"),
            model_version=d.get("model_version"),
            response_id=d.get("response_id"),
            latency_ms=int(d.get("latency_ms", 0)),
            cached=cached,
        )


class LLMError(Exception):
    def __init__(self, provider: str, model: str, status: int | None, message: str) -> None:
        super().__init__(f"{provider}/{model}: HTTP {status}: {message}")
        self.provider = provider
        self.model = model
        self.status = status
        self.message = message


class RateLimited(LLMError):
    """429. `retry_after` is the provider's hint in seconds, when it gives one."""

    def __init__(self, provider: str, model: str, message: str, retry_after: float | None) -> None:
        super().__init__(provider, model, 429, message)
        self.retry_after = retry_after


class AllTargetsExhausted(Exception):
    """Every model in a role's chain is rate limited, over budget, or failing."""
