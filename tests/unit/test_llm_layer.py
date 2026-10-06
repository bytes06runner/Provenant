"""LLM layer: provider adapters, budgets, replay cache, router fallback. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from llm import build_providers
from llm.budget import BudgetTracker, Limits
from llm.cache import ReplayCache, cache_key
from llm.config import CachePolicy, LLMConfig, LLMConfigError, Target, api_key, load_llm_config
from llm.providers import GeminiProvider, OpenAICompatibleProvider, _seconds
from llm.router import LLMRouter, RouterSettings, estimate_tokens
from llm.types import (
    AllTargetsExhausted,
    Image,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    RateLimited,
    Usage,
)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
REQ = LLMRequest(messages=(Message("system", "be terse"), Message("user", "hello")))


def engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def mock(handler):
    return httpx.MockTransport(handler)


# ---- adapters ---------------------------------------------------------------------


def test_openai_compatible_request_and_response_mapping():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "id": "r1",
                "system_fingerprint": "fp_1",
                "choices": [{"message": {"content": '{"a":1}'}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    p = OpenAICompatibleProvider("groq", "https://g/v1/", "k1", timeout=5, transport=mock(handler))
    img = Image(b"\x89PNG", "image/png")
    r = p.complete(
        "m1",
        LLMRequest(
            messages=(Message("user", "look", (img,)),),
            temperature="0.7",
            seed=42,
            json_schema={"type": "object"},
            schema_name="plan",
            options={"reasoning_effort": "low"},
        ),
    )
    body = seen["body"]
    assert seen["auth"] == "Bearer k1"
    assert body["temperature"] == 0.7 and body["seed"] == 42 and body["reasoning_effort"] == "low"
    assert body["response_format"]["json_schema"] == {
        "name": "plan",
        "schema": {"type": "object"},
        "strict": True,
    }
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert r.text == '{"a":1}' and r.model_version == "fp_1" and r.response_id == "r1"
    assert r.usage == Usage(10, 5, 3, 15) and r.finish_reason == "stop"


def test_openai_compatible_errors():
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow"}}),
            httpx.Response(400, json={"error": {"message": "bad schema"}}),
            httpx.Response(500, text="oops"),
        ]
    )
    p = OpenAICompatibleProvider(
        "groq", "https://g", "k", timeout=5, transport=mock(lambda r: next(responses))
    )
    with pytest.raises(RateLimited) as e:
        p.complete("m", REQ)
    assert e.value.retry_after == 7.0
    with pytest.raises(LLMError, match="bad schema") as e2:
        p.complete("m", REQ)
    assert e2.value.status == 400
    with pytest.raises(LLMError, match="oops"):
        p.complete("m", REQ)


def test_transport_errors_become_llm_errors():
    def boom(req):
        raise httpx.ConnectError("down")

    for cls in (OpenAICompatibleProvider, GeminiProvider):
        p = cls("x", "https://x", "k", timeout=5, transport=mock(boom))
        with pytest.raises(LLMError) as e:
            p.complete("m", REQ)
        assert e.value.status is None


def test_gemini_request_and_response_mapping():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["key"] = req.headers["x-goog-api-key"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "responseId": "g1",
                "modelVersion": "gemini-x-001",
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "thinking...", "thought": True}, {"text": "navy"}]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 300,
                    "candidatesTokenCount": 2,
                    "thoughtsTokenCount": 40,
                    "totalTokenCount": 342,
                },
            },
        )

    p = GeminiProvider("gemini", "https://gm/v1beta", "gk", timeout=5, transport=mock(handler))
    r = p.complete(
        "gemini-x",
        LLMRequest(
            messages=(
                Message("system", "sys"),
                Message("user", "what color?", (Image(b"jpg", "image/jpeg"),)),
                Message("assistant", "earlier"),
            ),
            seed=7,
            json_schema={"type": "object"},
        ),
    )
    body = seen["body"]
    assert seen["path"] == "/v1beta/models/gemini-x:generateContent" and seen["key"] == "gk"
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/jpeg"
    assert body["contents"][1]["role"] == "model"
    gc = body["generationConfig"]
    assert gc["seed"] == 7 and gc["responseMimeType"] == "application/json"
    assert gc["responseJsonSchema"] == {"type": "object"}
    assert (
        r.text == "navy" and r.usage == Usage(300, 2, 40, 342) and r.model_version == "gemini-x-001"
    )


def test_gemini_errors_and_retry_hint():
    responses = iter(
        [
            httpx.Response(
                429,
                json={
                    "error": {
                        "message": "quota",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": "31s",
                            }
                        ],
                    }
                },
            ),
            httpx.Response(429, json={"error": {"message": "quota"}}),
            httpx.Response(429, text="not json"),
            httpx.Response(403, json={"error": {"message": "denied"}}),
            httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
        ]
    )
    p = GeminiProvider(
        "gemini", "https://gm", "k", timeout=5, transport=mock(lambda r: next(responses))
    )
    with pytest.raises(RateLimited) as e:
        p.complete("m", REQ)
    assert e.value.retry_after == 31.0
    with pytest.raises(RateLimited) as e:
        p.complete("m", REQ)
    assert e.value.retry_after is None
    with pytest.raises(RateLimited):
        p.complete("m", REQ)
    with pytest.raises(LLMError, match="denied"):
        p.complete("m", REQ)
    with pytest.raises(LLMError, match="SAFETY"):
        p.complete("m", REQ)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30", 30.0), ("1.5s", 1.5), ("1m26.4s", 86.4), (None, None), ("soon", None)],
)
def test_duration_parsing(text, seconds):
    assert _seconds(text) == seconds


def test_error_message_shapes():
    from llm.providers import _error_message

    assert _error_message(httpx.Response(400, json={"error": "flat string"})) == "flat string"
    assert _error_message(httpx.Response(400, json={"message": "top level"})) == "top level"


# ---- budgets ----------------------------------------------------------------------


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def tracker(clock: Clock, **lim) -> BudgetTracker:
    limits = Limits(
        tokens_per_minute=lim.get("tpm", 1000),
        requests_per_day=lim.get("rpd", 10),
        tokens_per_day=lim.get("tpd", 5000),
    )
    return BudgetTracker(engine(), {"m": limits}, limits, now=clock)


def test_minute_window_wait_then_frees():
    clock = Clock(T0)
    b = tracker(clock)
    b.record("groq", "m", "planner", 700)
    clock.t = T0 + timedelta(seconds=10)
    b.record("groq", "m", "planner", 200)
    assert b.wait_seconds("m", 50) == 0.0
    assert b.wait_seconds("m", 400) == pytest.approx(50.0)  # first 700 leave at T0+60
    clock.t = T0 + timedelta(seconds=61)
    assert b.wait_seconds("m", 400) == 0.0


def test_daily_token_and_request_caps():
    clock = Clock(T0)
    b = tracker(clock, tpd=1000, rpd=2)
    b.record("groq", "m", "x", 600)
    assert b.wait_seconds("m", 500) is None  # 600 + 500 > 1000 today
    clock.t = T0 + timedelta(minutes=5)
    b.record("groq", "m", "x", 10)
    assert b.wait_seconds("m", 10) is None  # third request today
    clock.t = T0 + timedelta(days=1)
    assert b.wait_seconds("m", 10) == 0.0  # new UTC day


def test_call_larger_than_minute_allowance_never_fits():
    b = tracker(Clock(T0), tpm=100)
    assert b.wait_seconds("m", 101) is None


def test_unknown_model_uses_default_limits():
    b = tracker(Clock(T0))
    assert b.limits_for("other") == b.default
    assert b.spend("other").requests_today == 0


# ---- replay cache -------------------------------------------------------------------


def test_cache_key_changes_with_any_input():
    base = cache_key("groq", "m", REQ)
    variants = [
        cache_key("gemini", "m", REQ),
        cache_key("groq", "m2", REQ),
        cache_key("groq", "m", REQ, sample_index=1),
        cache_key("groq", "m", LLMRequest(messages=(Message("user", "hello!"),))),
        cache_key("groq", "m", LLMRequest(messages=REQ.messages, temperature="0.5")),
        cache_key("groq", "m", LLMRequest(messages=REQ.messages, seed=1)),
        cache_key("groq", "m", LLMRequest(messages=REQ.messages, json_schema={"type": "string"})),
        cache_key(
            "groq",
            "m",
            LLMRequest(messages=(Message("user", "hello", (Image(b"a", "image/png"),)),)),
        ),
    ]
    assert base == cache_key("groq", "m", REQ)
    assert len({base, *variants}) == len(variants) + 1


def resp(text="ok", tokens=10, provider="groq", model="m") -> LLMResponse:
    return LLMResponse(provider, model, text, Usage(5, 5, 0, tokens))


def test_cache_put_get_and_load_from_recorder():
    from blackbox.recorder import FlightRecorder

    eng = engine()
    cache = ReplayCache(eng)
    assert cache.get("k") is None
    cache.put("k", resp())
    cache.put("k", resp("ignored"))  # first write wins
    hit = cache.get("k")
    assert hit.text == "ok" and hit.cached

    rec = FlightRecorder(eng)
    rec.append(
        "s", "llm.call", {"cache_key": "k2", "cached": False, "response": resp("x").to_dict()}
    )
    rec.append(
        "s", "llm.call", {"cache_key": "k3", "cached": True, "response": resp("y").to_dict()}
    )
    rec.append("s", "plan.started", {"plan_hash": "h"})
    fresh = ReplayCache(engine())
    assert fresh.load_from_recorder(rec.events("s")) == 1
    assert fresh.get("k2").text == "x" and fresh.get("k3") is None


# ---- router ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, model, request):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return resp(out, provider=self.name, model=model)


def config(cache_policy=CachePolicy.NEVER) -> LLMConfig:
    lim = Limits(10_000, 100, 100_000)
    return LLMConfig(
        providers={"groq": {}, "gemini": {}},
        chains={"r": (Target("groq", "m1"), Target("gemini", "m2"))},
        cache={"r": cache_policy},
        limits={},
        default_limits=lim,
        http={},
    )


def router(providers, policy=CachePolicy.NEVER, events=None, clock=None, sleeps=None, cfg=None):
    eng = engine()
    cfg = cfg or config(policy)
    t = {"now": 0.0}
    return LLMRouter(
        cfg,
        providers,
        BudgetTracker(eng, cfg.limits, cfg.default_limits),
        ReplayCache(eng),
        record=(lambda k, p: events.append((k, p))) if events is not None else None,
        settings=RouterSettings(max_retries=2, backoff_base_seconds=1, default_cooldown_seconds=60),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        clock=clock or (lambda: t["now"]),
    )


def test_primary_serves_and_usage_is_recorded():
    events: list = []
    groq, gem = FakeProvider("groq", ["a"]), FakeProvider("gemini", [])
    r = router({"groq": groq, "gemini": gem}, events=events)
    out = r.call("r", REQ)
    assert out.text == "a" and gem.calls == 0
    assert r.budget.spend("m1").requests_today == 1
    ((kind, payload),) = events
    assert kind == "llm.call" and payload["target"] == "groq/m1" and payload["cached"] is False


def test_429_falls_back_and_cools_down():
    events: list = []
    now = {"t": 0.0}
    groq = FakeProvider("groq", [RateLimited("groq", "m1", "slow", 30.0), "later"])
    gem = FakeProvider("gemini", ["b", "c"])
    r = router({"groq": groq, "gemini": gem}, events=events, clock=lambda: now["t"])
    assert r.call("r", REQ).text == "b"
    assert r.call("r", REQ).text == "c" and groq.calls == 1  # still cooling down
    now["t"] = 31.0
    assert r.call("r", REQ).text == "later"
    assert [k for k, _ in events].count("llm.rate_limited") == 1


def test_429_without_hint_uses_default_cooldown():
    now = {"t": 0.0}
    groq = FakeProvider("groq", [RateLimited("groq", "m1", "slow", None), "x"])
    gem = FakeProvider("gemini", ["b", "c"])
    r = router({"groq": groq, "gemini": gem}, clock=lambda: now["t"])
    r.call("r", REQ)
    now["t"] = 59.0
    assert r.call("r", REQ).text == "c"
    now["t"] = 61.0
    assert r.call("r", REQ).text == "x"


def test_5xx_retries_then_falls_back():
    sleeps: list = []
    groq = FakeProvider("groq", [LLMError("groq", "m1", 503, "busy")] * 3)
    gem = FakeProvider("gemini", ["b"])
    r = router({"groq": groq, "gemini": gem}, sleeps=sleeps)
    assert r.call("r", REQ).text == "b"
    assert groq.calls == 3 and sleeps == [1, 2]


def test_transient_5xx_then_success_on_same_target():
    groq = FakeProvider("groq", [LLMError("groq", "m1", None, "reset"), "ok"])
    r = router({"groq": groq, "gemini": FakeProvider("gemini", [])})
    assert r.call("r", REQ).text == "ok"


def test_client_errors_are_raised_not_hidden_by_fallback():
    groq = FakeProvider("groq", [LLMError("groq", "m1", 400, "schema not supported")])
    gem = FakeProvider("gemini", ["b"])
    with pytest.raises(LLMError, match="schema"):
        router({"groq": groq, "gemini": gem}).call("r", REQ)
    assert gem.calls == 0


def test_all_targets_exhausted():
    groq = FakeProvider("groq", [RateLimited("groq", "m1", "slow", 5.0)])
    gem = FakeProvider("gemini", [RateLimited("gemini", "m2", "quota", 5.0)])
    with pytest.raises(AllTargetsExhausted, match="429"):
        router({"groq": groq, "gemini": gem}).call("r", REQ)


def test_daily_budget_spent_falls_back():
    cfg = config()
    cfg = LLMConfig(
        cfg.providers,
        cfg.chains,
        cfg.cache,
        {"m1": Limits(10_000, 1, 100_000)},
        cfg.default_limits,
        {},
    )
    groq, gem = FakeProvider("groq", ["a"]), FakeProvider("gemini", ["b"])
    r = router({"groq": groq, "gemini": gem}, cfg=cfg)
    assert r.call("r", REQ).text == "a"
    assert r.call("r", REQ).text == "b"  # groq used its 1 request/day


def test_short_minute_wait_sleeps_long_wait_falls_back():
    cfg = config()
    tight = Limits(
        tokens_per_minute=estimate_tokens(REQ) + 5, requests_per_day=100, tokens_per_day=100_000
    )
    cfg = LLMConfig(cfg.providers, cfg.chains, cfg.cache, {"m1": tight}, cfg.default_limits, {})
    sleeps: list = []
    groq, gem = FakeProvider("groq", ["a", "a2"]), FakeProvider("gemini", ["b"])
    r = router({"groq": groq, "gemini": gem}, cfg=cfg, sleeps=sleeps)
    r.call("r", REQ)  # uses 10 tokens of the window
    r.settings.max_wait_seconds = 0.0
    assert r.call("r", REQ).text == "b"  # window busy, wait not allowed: fall back
    r.settings.max_wait_seconds = 120.0
    assert r.call("r", REQ).text == "a2" and sleeps and 0 < sleeps[-1] <= 60


def test_cache_reuse_only_for_allowed_roles():
    events: list = []
    groq = FakeProvider("groq", ["first", "second"])
    r = router(
        {"groq": groq, "gemini": FakeProvider("gemini", [])},
        policy=CachePolicy.ALWAYS,
        events=events,
    )
    a = r.call("r", REQ)
    b = r.call("r", REQ)
    assert (a.text, b.text, b.cached, groq.calls) == ("first", "first", True, 1)
    assert events[-1][1]["cached"] is True
    c = r.call("r", REQ, sample_index=1)  # a different sample is a different call
    assert c.text == "second" and groq.calls == 2

    groq2 = FakeProvider("groq", ["x", "y"])
    r2 = router({"groq": groq2, "gemini": FakeProvider("gemini", [])}, policy=CachePolicy.NEVER)
    assert [r2.call("r", REQ).text, r2.call("r", REQ).text] == ["x", "y"]


def test_cache_hit_from_fallback_target():
    """A response served by the fallback earlier is reused even when the primary recovers."""
    now = {"t": 0.0}
    groq = FakeProvider("groq", [RateLimited("groq", "m1", "slow", 5.0), "fresh"])
    gem = FakeProvider("gemini", ["from-fallback"])
    r = router({"groq": groq, "gemini": gem}, policy=CachePolicy.ALWAYS, clock=lambda: now["t"])
    assert r.call("r", REQ).text == "from-fallback"
    now["t"] = 10.0
    again = r.call("r", REQ)
    assert again.text == "from-fallback" and again.cached and groq.calls == 1


def test_estimate_counts_images_and_output_allowance():
    with_img = LLMRequest(
        messages=(Message("user", "x" * 300, (Image(b"1", "image/png"),)),), max_tokens=100
    )
    assert estimate_tokens(with_img) == 100 + 100 + 600


# ---- config --------------------------------------------------------------------------

YAML = """
providers:
  groq: {kind: openai_compatible, base_url: "https://g/v1", api_key_env: GROQ_API_KEY}
  gemini: {kind: gemini, base_url: "https://gm/v1beta", api_key_env: GEMINI_API_KEY}
roles: [planner, extractor]
http: {timeout_seconds: 5, max_retries: 1, backoff_base_seconds: 1, default_cooldown_seconds: 60}
budgets:
  default: {tokens_per_minute: 8000, requests_per_day: 1000, tokens_per_day: 150000}
  models:
    m1: {tokens_per_minute: 10, requests_per_day: 1, tokens_per_day: 100}
cache: {extractor: always}
"""

ENV = {
    "LLM_PLANNER_PROVIDER": "groq",
    "LLM_PLANNER_MODEL": "m1",
    "LLM_PLANNER_FALLBACK_PROVIDER": "gemini",
    "LLM_PLANNER_FALLBACK_MODEL": "m2",
    "LLM_EXTRACTOR_PROVIDER": "groq",
    "LLM_EXTRACTOR_MODEL": "q1",
    "GROQ_API_KEY": "gk",
    "GEMINI_API_KEY": "mk",
}


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    p = tmp_path / "llm.yaml"
    p.write_text(YAML)
    return p


def test_config_loads_chains_policies_and_limits(yaml_file):
    cfg = load_llm_config(yaml_file, ENV)
    assert [t.label() for t in cfg.chain("planner")] == ["groq/m1", "gemini/m2"]
    assert cfg.cache == {"planner": CachePolicy.NEVER, "extractor": CachePolicy.ALWAYS}
    assert cfg.limits["m1"].requests_per_day == 1
    with pytest.raises(LLMConfigError, match="unknown"):
        cfg.chain("poet")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"LLM_EXTRACTOR_MODEL": ""}, "both be set"),
        ({"LLM_EXTRACTOR_PROVIDER": "openai"}, "not in config"),
        ({"LLM_EXTRACTOR_PROVIDER": "", "LLM_EXTRACTOR_MODEL": ""}, "no model configured"),
    ],
)
def test_config_errors(yaml_file, change, match):
    with pytest.raises(LLMConfigError, match=match):
        load_llm_config(yaml_file, {**ENV, **change})


def test_api_key_and_provider_factory(yaml_file):
    cfg = load_llm_config(yaml_file, ENV)
    assert api_key(cfg, "groq", ENV) == "gk"
    with pytest.raises(LLMConfigError, match="GEMINI_API_KEY"):
        api_key(cfg, "gemini", {})
    providers = build_providers(cfg, ENV)
    assert set(providers) == {"groq", "gemini"}
    assert isinstance(providers["gemini"], GeminiProvider)
    bad = LLMConfig(
        {**cfg.providers, "groq": {**cfg.providers["groq"], "kind": "carrier-pigeon"}},
        cfg.chains,
        cfg.cache,
        cfg.limits,
        cfg.default_limits,
        cfg.http,
    )
    with pytest.raises(ValueError, match="kind"):
        build_providers(bad, ENV)


def test_replay_settings_k_is_per_run():
    from blackbox.replay import ReplaySettings

    assert ReplaySettings.for_run().k == 4  # config/app.yaml default (development, eval)
    assert ReplaySettings.for_demo().k == 8  # the recorded demo case
    assert ReplaySettings.for_run(k=3).k == 3
    with pytest.raises(ValueError):
        ReplaySettings(0)


def test_build_router_from_real_config(monkeypatch):
    """The factory wires the repo's config/llm.yaml with env-provided keys and models."""
    from llm import build_router

    for k, v in {
        **ENV,
        "LLM_MANDATE_PROVIDER": "groq",
        "LLM_MANDATE_MODEL": "m",
        "LLM_VISION_PROVIDER": "gemini",
        "LLM_VISION_MODEL": "v",
        "LLM_NARRATOR_PROVIDER": "gemini",
        "LLM_NARRATOR_MODEL": "n",
        "LLM_REFERENCE_POLICY_PROVIDER": "gemini",
        "LLM_REFERENCE_POLICY_MODEL": "r",
        "LLM_BASELINE_PROVIDER": "gemini",
        "LLM_BASELINE_MODEL": "b",
    }.items():
        monkeypatch.setenv(k, v)
    r = build_router(engine())
    assert set(r.providers) == {"groq", "gemini"}
    assert r.config.cache["extractor"] is CachePolicy.ALWAYS


def test_gemini_without_system_message_and_mixed_error_details():
    seen = {}
    responses = iter(
        [
            httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
            httpx.Response(
                429,
                json={
                    "error": {
                        "message": "q",
                        "details": [
                            {"@type": "type.googleapis.com/google.rpc.QuotaFailure"},
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": "5s",
                            },
                        ],
                    }
                },
            ),
        ]
    )

    def handler(req):
        seen["body"] = json.loads(req.content)
        return next(responses)

    p = GeminiProvider("gemini", "https://gm", "k", timeout=5, transport=mock(handler))
    out = p.complete("m", LLMRequest(messages=(Message("user", "hi"),)))
    assert "systemInstruction" not in seen["body"] and out.usage == Usage()
    with pytest.raises(RateLimited) as e:
        p.complete("m", REQ)
    assert e.value.retry_after == 5.0


def test_minute_window_frees_after_several_entries():
    clock = Clock(T0)
    b = tracker(clock, tpm=100)
    for i in range(3):
        clock.t = T0 + timedelta(seconds=i)
        b.record("groq", "m", "x", 40)
    clock.t = T0 + timedelta(seconds=5)
    # 120 used, need 70 under a 100 limit: all three 40s must expire; the last (T0+2) leaves
    # the window at T0+62, which is 57 s after T0+5.
    assert b.wait_seconds("m", 70) == pytest.approx(57.0)


def test_never_cache_role_ignores_entries_written_by_other_roles():
    """The cache key has no role, so a cacheable role on the same model can leave an entry for
    identical inputs. A never-cache role (the sampled planner) must still call the model."""
    groq = FakeProvider("groq", ["live"])
    r = router({"groq": groq, "gemini": FakeProvider("gemini", [])}, policy=CachePolicy.NEVER)
    r.cache.put(cache_key("groq", "m1", REQ), resp("stale-from-another-role"))
    out = r.call("r", REQ)
    assert out.text == "live" and not out.cached and groq.calls == 1


@pytest.mark.parametrize(
    ("mode", "groq_format", "gemini_has_schema"),
    [
        ("strict", {"type": "json_schema", "strict": True}, True),
        ("schema", {"type": "json_schema", "strict": False}, True),
        ("json", {"type": "json_object"}, False),
    ],
)
def test_structured_output_modes(mode, groq_format, gemini_has_schema):
    bodies = []

    def ok_openai(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    def ok_gemini(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})

    request = LLMRequest(messages=REQ.messages, json_schema={"type": "object"}, structured=mode)
    OpenAICompatibleProvider("g", "https://g", "k", timeout=5, transport=mock(ok_openai)).complete(
        "m", request
    )
    GeminiProvider("m", "https://m", "k", timeout=5, transport=mock(ok_gemini)).complete(
        "m", request
    )
    fmt = bodies[0]["response_format"]
    assert fmt["type"] == groq_format["type"]
    if "strict" in groq_format:
        assert fmt["json_schema"]["strict"] is groq_format["strict"]
    gc = bodies[1]["generationConfig"]
    assert gc["responseMimeType"] == "application/json"
    assert ("responseJsonSchema" in gc) is gemini_has_schema
    assert cache_key("g", "m", request) != cache_key(
        "g", "m", LLMRequest(messages=REQ.messages, json_schema={"type": "object"}, structured="x")
    )


def test_budget_warning_at_80_percent_fires_once_per_day():
    events: list = []
    cfg = config()
    cfg = LLMConfig(
        cfg.providers,
        cfg.chains,
        cfg.cache,
        {"m1": Limits(100_000, 5, 100_000)},
        cfg.default_limits,
        {},
    )
    groq = FakeProvider("groq", ["a"] * 5)
    r = router({"groq": groq, "gemini": FakeProvider("gemini", [])}, cfg=cfg, events=events)
    for _ in range(5):
        r.call("r", REQ)
    warnings = [p for k, p in events if k == "llm.budget_warning"]
    assert len(warnings) == 1
    assert warnings[0]["metric"] == "requests_per_day" and warnings[0]["used"] == 4
    assert warnings[0]["limit"] == 5 and warnings[0]["threshold"] == "0.8"


def test_token_budget_warning():
    events: list = []
    cfg = config()
    cfg = LLMConfig(
        cfg.providers,
        cfg.chains,
        cfg.cache,
        {"m1": Limits(100_000, 100, 12)},
        cfg.default_limits,
        {},
    )
    r = router(
        {"groq": FakeProvider("groq", ["a"]), "gemini": FakeProvider("gemini", [])},
        cfg=cfg,
        events=events,
    )
    r.budget.limits["m1"] = Limits(100_000, 100, 10_000)  # let the call through
    r.call("r", REQ)
    r.budget.limits["m1"] = Limits(100_000, 100, 12)  # 10 tokens used of 12: over 80%
    r._warn_if_near_budget("r", Target("groq", "m1"))
    assert [p["metric"] for k, p in events if k == "llm.budget_warning"] == ["tokens_per_day"]


def test_quota_429_teaches_the_router_the_real_daily_limit():
    events: list = []
    quota = {"id": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "value": "20"}
    groq = FakeProvider("groq", [RateLimited("groq", "m1", "quota", 50000.0, quota)])
    gem = FakeProvider("gemini", ["b"])
    r = router({"groq": groq, "gemini": gem}, events=events)
    assert r.call("r", REQ).text == "b"
    assert r.budget.limits_for("m1").requests_per_day == 20
    (q,) = [p for k, p in events if k == "llm.quota_discovered"]
    assert q["requests_per_day"] == 20 and q["target"] == "groq/m1"


@pytest.mark.parametrize(
    "quota",
    [None, {"id": "GenerateRequestsPerMinute", "value": "15"}, {"id": "PerDay", "value": "n/a"}],
)
def test_non_daily_or_malformed_quotas_are_ignored(quota):
    groq = FakeProvider("groq", [RateLimited("groq", "m1", "q", 5.0, quota)])
    r = router({"groq": groq, "gemini": FakeProvider("gemini", ["b"])})
    before = r.budget.limits_for("m1")
    r.call("r", REQ)
    assert r.budget.limits_for("m1") == before


def test_same_quota_value_is_not_reannounced():
    events: list = []
    quota = {"id": "RequestsPerDay", "value": "100"}  # equals the current limit
    groq = FakeProvider("groq", [RateLimited("groq", "m1", "q", 5.0, quota)])
    r = router({"groq": groq, "gemini": FakeProvider("gemini", ["b"])}, events=events)
    r.call("r", REQ)
    assert not [k for k, _ in events if k == "llm.quota_discovered"]


def test_gemini_quota_failure_is_parsed():
    body = {
        "error": {
            "message": "quota",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                            "quotaValue": "20",
                        }
                    ],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "52758s"},
            ],
        }
    }
    responses = iter(
        [
            httpx.Response(429, json=body),
            httpx.Response(429, text="x"),
            httpx.Response(
                429,
                json={
                    "error": {
                        "details": [{"@type": "x.QuotaFailure", "violations": [{"quotaId": "a"}]}]
                    }
                },
            ),
        ]
    )
    p = GeminiProvider(
        "gemini", "https://gm", "k", timeout=5, transport=mock(lambda r: next(responses))
    )
    with pytest.raises(RateLimited) as e:
        p.complete("m", REQ)
    assert e.value.quota == {
        "id": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        "value": "20",
    }
    assert e.value.retry_after == 52758.0
    for _ in range(2):
        with pytest.raises(RateLimited) as e:
            p.complete("m", REQ)
        assert e.value.quota is None


def test_call_defaults_from_config(yaml_file):
    cfg = load_llm_config(yaml_file, ENV)
    assert cfg.call_defaults("planner") == {} and cfg.warning_fraction == Decimal("0.8")
    real = load_llm_config()
    assert real.call_defaults("planner")["max_tokens"] == 1200


def test_requests_per_minute_and_pacific_quota_day():
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from llm.budget import BudgetTracker, Limits

    t = [datetime(2026, 10, 6, 6, 59, 0, tzinfo=UTC)]  # 23:59 Pacific on Oct 5
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    lim = Limits(1000, 3, 10_000, requests_per_minute=2, day_reset_tz="America/Los_Angeles")
    b = BudgetTracker(engine, {"g": lim}, lim, now=lambda: t[0])
    b.record("gemini", "g", "planner", 10)
    t[0] += timedelta(seconds=10)
    b.record("gemini", "g", "planner", 10)
    assert b.wait_seconds("g", 10) == pytest.approx(50.0)  # third request waits for the first
    t[0] += timedelta(seconds=55)  # 07:00:05 UTC: a new Pacific day; the first call aged out
    assert b.spend("g").requests_today == 0 and b.spend("g").requests_last_minute == 1
    assert b.wait_seconds("g", 10) == 0.0
    assert b.day_start("g") == datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
    utc = BudgetTracker(engine, {}, Limits(1000, 3, 10_000), now=lambda: t[0])
    assert utc.day_start("g") == datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    assert utc.spend("g").requests_today == 2  # the UTC day still counts both calls


def test_rpm_and_token_windows_combine():
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from llm.budget import BudgetTracker, Limits

    t = [datetime(2026, 10, 6, 12, 0, tzinfo=UTC)]
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    lim = Limits(100, 50, 10_000, requests_per_minute=1)
    b = BudgetTracker(engine, {"g": lim}, lim, now=lambda: t[0])
    b.record("gemini", "g", "planner", 90)
    t[0] += timedelta(seconds=20)
    assert b.wait_seconds("g", 50) == pytest.approx(40.0)  # both windows free at the same time
