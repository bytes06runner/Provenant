"""Provider-agnostic LLM layer: Groq (OpenAI-compatible) primary, Gemini secondary.

    router = build_router(engine)
    response = router.call("extractor", LLMRequest(messages=(...,), json_schema=...))

Every call goes through `LLMRouter`: per-model budgets, 429 fallback, replay cache, and an
`llm.call` event per call for the Flight Recorder.
"""

from __future__ import annotations

import os

from sqlalchemy.engine import Engine

from llm.budget import BudgetTracker
from llm.cache import ReplayCache
from llm.config import LLMConfig, api_key, load_llm_config
from llm.providers import GeminiProvider, OpenAICompatibleProvider, Provider
from llm.router import LLMRouter, Recorder, RouterSettings
from llm.types import AllTargetsExhausted, Image, LLMError, LLMRequest, LLMResponse, Message

__all__ = [
    "AllTargetsExhausted",
    "Image",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "LLMRouter",
    "Message",
    "build_providers",
    "build_router",
]


def build_providers(config: LLMConfig, env: dict[str, str] | None = None) -> dict[str, Provider]:
    env = env if env is not None else dict(os.environ)
    used = {t.provider for chain in config.chains.values() for t in chain}
    timeout = float(config.http["timeout_seconds"])
    out: dict[str, Provider] = {}
    for name in sorted(used):
        spec = config.providers[name]
        key = api_key(config, name, env)
        if spec["kind"] == "openai_compatible":
            out[name] = OpenAICompatibleProvider(name, spec["base_url"], key, timeout=timeout)
        elif spec["kind"] == "gemini":
            out[name] = GeminiProvider(name, spec["base_url"], key, timeout=timeout)
        else:
            raise ValueError(f"unknown provider kind {spec['kind']!r}")
    return out


def build_router(engine: Engine, *, record: Recorder | None = None) -> LLMRouter:
    config = load_llm_config()
    http = config.http
    return LLMRouter(
        config,
        build_providers(config),
        BudgetTracker(engine, config.limits, config.default_limits),
        ReplayCache(engine),
        record=record,
        settings=RouterSettings(
            max_retries=int(http["max_retries"]),
            backoff_base_seconds=float(http["backoff_base_seconds"]),
            default_cooldown_seconds=float(http["default_cooldown_seconds"]),
        ),
    )
