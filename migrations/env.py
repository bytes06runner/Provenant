"""Alembic environment: every Provenant table, from the modules that define them."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blackbox.reconcile import metadata as reconcile  # noqa: E402
from blackbox.recorder import metadata as recorder  # noqa: E402
from lineage.nonces import metadata as nonces  # noqa: E402
from lineage.vault import metadata as vault  # noqa: E402
from llm.budget import metadata as budget  # noqa: E402
from llm.cache import metadata as cache  # noqa: E402
from paypal.config import load_env  # noqa: E402
from paypal.ledger import metadata as ledger  # noqa: E402
from paypal.storage import database_url  # noqa: E402
from paypal.webhooks import metadata as webhooks  # noqa: E402

target_metadata = [recorder, ledger, webhooks, nonces, vault, budget, cache, reconcile]


def _url() -> str:
    load_env()
    url = database_url("")
    if not url:
        raise SystemExit("set PROVENANT_DATABASE_URL to migrate")
    return url


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
