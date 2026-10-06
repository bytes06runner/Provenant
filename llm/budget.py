"""Per-model token and request budgets, persisted so they hold across processes and restarts.

Limits per model (config/llm.yaml): tokens per minute and, optionally, requests per minute
(sliding 60 s windows), requests per day and tokens per day. The day ends where the provider's
quota resets: UTC by default, America/Los_Angeles for Gemini (`day_reset_tz`). Before a call
the router asks how long it must wait for this model; `None` means the daily budget is spent
and the router must fall back.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, func, insert, select
from sqlalchemy.engine import Engine

from paypal.storage import ensure_schema

metadata = MetaData()

llm_usage = Table(
    "llm_usage",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(32), nullable=False),
    Column("model", String(128), nullable=False, index=True),
    Column("role", String(32), nullable=False),
    Column("tokens", Integer, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False, index=True),
)


@dataclass(frozen=True)
class Limits:
    tokens_per_minute: int
    requests_per_day: int
    tokens_per_day: int
    requests_per_minute: int | None = None
    day_reset_tz: str = "UTC"


@dataclass(frozen=True)
class Spend:
    tokens_today: int
    requests_today: int
    tokens_last_minute: int
    requests_last_minute: int = 0


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class BudgetTracker:
    def __init__(
        self,
        engine: Engine,
        limits: dict[str, Limits],
        default: Limits,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.limits = limits
        self.default = default
        self.now = now
        ensure_schema(metadata, engine)  # SQLite only; Alembic owns Postgres

    def limits_for(self, model: str) -> Limits:
        return self.limits.get(model, self.default)

    def day_start(self, model: str) -> datetime:
        """Start of the provider's current quota day, in UTC."""
        local = self.now().astimezone(ZoneInfo(self.limits_for(model).day_reset_tz))
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    def spend(self, model: str) -> Spend:
        now = self.now()
        day_start = self.day_start(model)
        minute_ago = now - timedelta(seconds=60)
        q_day = select(func.coalesce(func.sum(llm_usage.c.tokens), 0), func.count()).where(
            llm_usage.c.model == model, llm_usage.c.at >= day_start
        )
        q_min = select(func.coalesce(func.sum(llm_usage.c.tokens), 0), func.count()).where(
            llm_usage.c.model == model, llm_usage.c.at >= minute_ago
        )
        with self.engine.connect() as conn:
            tokens_today, requests_today = conn.execute(q_day).one()
            tokens_minute, requests_minute = conn.execute(q_min).one()
        return Spend(
            int(tokens_today), int(requests_today), int(tokens_minute), int(requests_minute)
        )

    def wait_seconds(self, model: str, estimated_tokens: int) -> float | None:
        """0 if the call fits now, seconds to wait if only the minute window is full, None if
        today's budget cannot cover it."""
        lim = self.limits_for(model)
        s = self.spend(model)
        if s.requests_today + 1 > lim.requests_per_day:
            return None
        if s.tokens_today + estimated_tokens > lim.tokens_per_day:
            return None
        if estimated_tokens > lim.tokens_per_minute:
            return None  # can never fit in one minute's allowance
        rpm_wait = 0.0
        if (
            lim.requests_per_minute is not None
            and s.requests_last_minute + 1 > lim.requests_per_minute
        ):
            rpm_wait = self._seconds_until_requests_free(model, lim.requests_per_minute)
        if s.tokens_last_minute + estimated_tokens <= lim.tokens_per_minute:
            return rpm_wait
        return max(rpm_wait, self._seconds_until_minute_frees(model, estimated_tokens, lim))

    def _seconds_until_requests_free(self, model: str, rpm: int) -> float:
        """When the oldest request still blocking the per-minute request count leaves the window."""
        now = self.now()
        q = (
            select(llm_usage.c.at)
            .where(llm_usage.c.model == model, llm_usage.c.at >= now - timedelta(seconds=60))
            .order_by(llm_usage.c.at)
        )
        with self.engine.connect() as conn:
            times = [r[0] for r in conn.execute(q)]
        blocking = times[len(times) - rpm]  # the call fits once this one is older than 60 s
        return max(0.0, (_aware(blocking) + timedelta(seconds=60) - now).total_seconds())

    def _seconds_until_minute_frees(self, model: str, need: int, lim: Limits) -> float:
        now = self.now()
        q = (
            select(llm_usage.c.tokens, llm_usage.c.at)
            .where(llm_usage.c.model == model, llm_usage.c.at >= now - timedelta(seconds=60))
            .order_by(llm_usage.c.at)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        used = sum(t for t, _ in rows)
        for tokens, at in rows:
            used -= tokens
            if used + need <= lim.tokens_per_minute:
                return max(0.0, (_aware(at) + timedelta(seconds=60) - now).total_seconds())
        return 60.0  # pragma: no cover  (unreachable: need <= limit is checked by the caller)

    def record(self, provider: str, model: str, role: str, tokens: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(llm_usage).values(
                    provider=provider, model=model, role=role, tokens=tokens, at=self.now()
                )
            )
