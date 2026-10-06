"""Where Provenant's tables live, and who creates them.

Postgres (staging and production): Alembic migrations own the schema (`alembic upgrade head`,
run at deploy); application code never creates or alters tables there.
SQLite (local development and tests): tables are created on first use, so a fresh checkout and
the unit tests need no migration step.

`PROVENANT_DATABASE_URL` selects the database; Render's `postgres://` and `postgresql://` URLs
are mapped to the psycopg 3 driver.
"""

from __future__ import annotations

import os

from sqlalchemy import MetaData
from sqlalchemy.engine import Engine


def database_url(default: str) -> str:
    url = os.environ.get("PROVENANT_DATABASE_URL", "").strip() or default
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def ensure_schema(metadata: MetaData, engine: Engine) -> None:
    """Create missing tables on SQLite only; on any other database Alembic owns the schema."""
    if engine.dialect.name == "sqlite":
        metadata.create_all(engine)
