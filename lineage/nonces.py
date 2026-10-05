"""Mandate nonce registry: a signed mandate can start exactly one purchase session."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

mandate_nonces = Table(
    "mandate_nonces",
    metadata,
    Column("user_id", String(64), primary_key=True),
    Column("nonce", String(64), primary_key=True),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
)


class SqlNonceRegistry:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata.create_all(engine)  # replaced by Alembic migrations in Phase 1

    def claim(self, user_id: str, nonce: str) -> bool:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(mandate_nonces).values(
                        user_id=user_id, nonce=nonce, claimed_at=datetime.now(UTC)
                    )
                )
        except IntegrityError:
            return False
        return True
