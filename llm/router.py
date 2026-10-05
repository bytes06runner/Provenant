"""LLM router: one entry point for every model call in Provenant.

For a role it walks the configured chain (primary, then fallbacks):
  1. Replay cache: if the role's policy allows reuse and these exact inputs were seen for a
     target in the chain, return the recorded response without calling anyone.
  2. Skip targets cooling down after a 429, or whose daily budget is spent. If only the
     per-minute window is full, wait when the wait is short, otherwise fall back.
  3. Call. Retry transport errors and 5xx with backoff; on 429 start a cooldown (provider hint,
     or the configured default) and fall back to the next target.
  4. Record usage against the budget, store the response for replay, and emit an `llm.call`
     event (model, usage, latency, cache key, response) for the Flight Recorder.

Errors that are not about availability (400 for a bad schema, 401) are raised immediately:
falling back would hide a configuration bug.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from llm.budget import BudgetTracker
from llm.cache import ReplayCache, cache_key
from llm.config import CachePolicy, LLMConfig, Target
from llm.providers import Provider
from llm.types import AllTargetsExhausted, LLMError, LLMRequest, LLMResponse, RateLimited

Recorder = Callable[[str, dict[str, Any]], None]
log = logging.getLogger("provenant.llm")


def estimate_tokens(request: LLMRequest) -> int:
    """Conservative pre-call estimate: about 3 characters per token, plus the output allowance.
    Images are counted at a flat 600 tokens each (Gemini bills roughly 258 to 1,100)."""
    images = sum(len(m.images) for m in request.messages)
    return request.text_chars() // 3 + request.max_tokens + 600 * images


@dataclass
class RouterSettings:
    max_wait_seconds: float = 20.0  # wait this long for a full minute window before falling back
    max_retries: int = 2
    backoff_base_seconds: float = 1.0
    default_cooldown_seconds: float = 60.0


@dataclass
class _Cooldowns:
    until: dict[str, float] = field(default_factory=dict)


class LLMRouter:
    def __init__(
        self,
        config: LLMConfig,
        providers: dict[str, Provider],
        budget: BudgetTracker,
        cache: ReplayCache,
        *,
        record: Recorder | None = None,
        settings: RouterSettings | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.providers = providers
        self.budget = budget
        self.cache = cache
        self.record = record or (lambda _t, _p: None)
        self.settings = settings or RouterSettings()
        self.sleep = sleep
        self.clock = clock
        self._cooldowns = _Cooldowns()
        self._warned: set[tuple[str, str, str]] = set()  # (model, metric, UTC day)

    def call(self, role: str, request: LLMRequest, *, sample_index: int = 0) -> LLMResponse:
        chain = self.config.chain(role)
        policy = self.config.cache.get(role, CachePolicy.NEVER)

        if policy is CachePolicy.ALWAYS:
            for target in chain:
                key = cache_key(target.provider, target.model, request, sample_index)
                hit = self.cache.get(key)
                if hit is not None:
                    self._emit(role, target, key, hit, request, sample_index)
                    return hit

        estimate = estimate_tokens(request)
        skipped: list[str] = []
        for target in chain:
            reason = self._unavailable(target, estimate)
            if reason:
                skipped.append(f"{target.label()}: {reason}")
                continue
            try:
                response = self._call_with_retries(target, request)
            except RateLimited as e:
                self._learn_quota(role, target, e)
                cool = e.retry_after or self.settings.default_cooldown_seconds
                self._cooldowns.until[target.label()] = self.clock() + cool
                skipped.append(f"{target.label()}: 429, cooling down {cool:.0f}s")
                self.record(
                    "llm.rate_limited",
                    {"role": role, "target": target.label(), "cooldown_seconds": str(cool)},
                )
                continue
            except LLMError as e:
                if e.status is None or e.status >= 500:
                    skipped.append(f"{target.label()}: {e.message}")
                    continue
                raise
            self.budget.record(target.provider, target.model, role, response.usage.total_tokens)
            self._warn_if_near_budget(role, target)
            key = cache_key(target.provider, target.model, request, sample_index)
            if policy is CachePolicy.ALWAYS:
                self.cache.put(key, response)
            self._emit(role, target, key, response, request, sample_index)
            return response
        raise AllTargetsExhausted(f"role {role!r}: " + "; ".join(skipped))

    # ---- budget visibility ---------------------------------------------------------

    def _warn_if_near_budget(self, role: str, target: Target) -> None:
        lim = self.budget.limits_for(target.model)
        spend = self.budget.spend(target.model)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        frac = self.config.warning_fraction
        for metric, used, limit in (
            ("requests_per_day", spend.requests_today, lim.requests_per_day),
            ("tokens_per_day", spend.tokens_today, lim.tokens_per_day),
        ):
            key = (target.model, metric, day)
            if used >= frac * limit and key not in self._warned:
                self._warned.add(key)
                log.warning("%s at %s of %s %s", target.label(), used, limit, metric)
                self.record(
                    "llm.budget_warning",
                    {
                        "role": role,
                        "target": target.label(),
                        "metric": metric,
                        "used": used,
                        "limit": limit,
                        "threshold": str(frac),
                    },
                )

    def _learn_quota(self, role: str, target: Target, e: RateLimited) -> None:
        """A provider's quota 429 states the real daily limit; adopt it immediately."""
        if not e.quota or "PerDay" not in e.quota["id"] or not e.quota["value"].isdigit():
            return
        value = int(e.quota["value"])
        current = self.budget.limits_for(target.model)
        if current.requests_per_day != value:
            self.budget.limits[target.model] = replace(current, requests_per_day=value)
            log.warning("%s daily request limit is %s (%s)", target.label(), value, e.quota["id"])
            self.record(
                "llm.quota_discovered",
                {
                    "role": role,
                    "target": target.label(),
                    "quota_id": e.quota["id"],
                    "requests_per_day": value,
                },
            )

    # ---- internals ---------------------------------------------------------------

    def _unavailable(self, target: Target, estimate: int) -> str | None:
        cooling = self._cooldowns.until.get(target.label(), 0.0) - self.clock()
        if cooling > 0:
            return f"cooling down {cooling:.0f}s after 429"
        wait = self.budget.wait_seconds(target.model, estimate)
        if wait is None:
            return "daily budget spent"
        if wait > self.settings.max_wait_seconds:
            return f"minute window full for {wait:.0f}s"
        if wait > 0:
            self.sleep(wait)
        return None

    def _call_with_retries(self, target: Target, request: LLMRequest) -> LLMResponse:
        provider = self.providers[target.provider]
        attempt = 0
        while True:
            try:
                return provider.complete(target.model, request)
            except RateLimited:
                raise
            except LLMError as e:
                retryable = e.status is None or e.status >= 500
                if not retryable or attempt >= self.settings.max_retries:
                    raise
                self.sleep(self.settings.backoff_base_seconds * (2**attempt))
                attempt += 1

    def _emit(
        self,
        role: str,
        target: Target,
        key: str,
        response: LLMResponse,
        request: LLMRequest,
        sample_index: int,
    ) -> None:
        self.record(
            "llm.call",
            {
                "role": role,
                "target": target.label(),
                "cache_key": key,
                "cached": response.cached,
                "sample_index": sample_index,
                "request": request.identity(),
                "response": response.to_dict(),
            },
        )
