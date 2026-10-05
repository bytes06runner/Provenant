"""Replay cache: reuse a recorded model response when a call's inputs are exactly the same.

Blackbox replays a purchase under interventions. Most steps see identical inputs in every
replay (the Q-LLM extracting the same page at temperature 0), so only the steps an intervention
actually changes should hit a model again. The cache key is the SHA-256 of the canonical
request identity plus provider, model and sample index; any change to a message, image, schema,
temperature or seed is a different key, so a changed step is always recomputed.

The Flight Recorder is the source of truth: every live call is recorded as an `llm.call` event
carrying its cache key and response, and `load_from_recorder` rebuilds the cache from a session.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Column, MetaData, String, Table, Text, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from lineage.canonical import content_hash
from llm.types import LLMRequest, LLMResponse

metadata = MetaData()

llm_cache = Table(
    "llm_cache",
    metadata,
    Column("cache_key", String(64), primary_key=True),
    Column("response_json", Text, nullable=False),
)


def cache_key(provider: str, model: str, request: LLMRequest, sample_index: int = 0) -> str:
    return content_hash(
        {
            "provider": provider,
            "model": model,
            "sample_index": sample_index,
            "request": request.identity(),
        }
    )


class ReplayCache:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata.create_all(engine)  # replaced by Alembic migrations in Phase 1

    def get(self, key: str) -> LLMResponse | None:
        q = select(llm_cache.c.response_json).where(llm_cache.c.cache_key == key)
        with self.engine.connect() as conn:
            row = conn.execute(q).scalar_one_or_none()
        return LLMResponse.from_dict(json.loads(row), cached=True) if row else None

    def put(self, key: str, response: LLMResponse) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(llm_cache).values(
                        cache_key=key, response_json=json.dumps(response.to_dict())
                    )
                )
        except IntegrityError:
            pass  # same inputs, already stored

    def load_from_recorder(self, events: Iterable[Any]) -> int:
        """Rebuild entries from a session's `llm.call` events. Returns how many were loaded."""
        loaded = 0
        for e in events:
            if e.event_type != "llm.call" or e.payload.get("cached"):
                continue
            payload = e.payload
            self.put(payload["cache_key"], LLMResponse.from_dict(payload["response"]))
            loaded += 1
        return loaded
