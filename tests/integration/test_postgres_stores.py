"""Every store on a real, Alembic-migrated Postgres (staging).

    PROVENANT_TEST_DATABASE_URL=postgresql://... pytest -m postgres

Rows are written under unique ids, so the test can run against a shared staging database.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, inspect

pytestmark = pytest.mark.postgres

URL = os.environ.get("PROVENANT_TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def engine():
    if not URL:
        pytest.skip("PROVENANT_TEST_DATABASE_URL is not set")
    from paypal.storage import database_url

    os.environ["PROVENANT_DATABASE_URL"] = URL
    e = create_engine(database_url(URL), pool_pre_ping=True)
    assert e.dialect.name == "postgresql"
    return e


def test_schema_is_migrated_not_created(engine):
    tables = set(inspect(engine).get_table_names())
    expected = {
        "alembic_version",
        "recorder_events",
        "recorder_blobs",
        "paypal_requests",
        "webhook_events",
        "mandate_nonces",
        "vault_addresses",
        "llm_usage",
        "llm_cache",
        "reconcile_tracked",
    }
    assert expected <= tables


def test_recorder_chain_blobs_and_concurrent_appends(engine):
    from blackbox.recorder import FlightRecorder

    rec = FlightRecorder(engine)
    s = f"t-{uuid.uuid4().hex[:10]}"
    digest = rec.put_blob(b"\x00\xffpdf bytes", "application/pdf")
    assert rec.get_blob(digest) == b"\x00\xffpdf bytes"

    def worker(i: int) -> None:
        for j in range(5):
            rec.append(s, "test.event", {"worker": i, "n": j})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = rec.events(s)
    assert [e.seq for e in events] == list(range(20))
    rec.verify(s)
    assert events[0].recorded_at.tzinfo is not None


def test_ledger_idempotency(engine):
    from paypal.ledger import OpState, RequestLedger

    ledger = RequestLedger(engine)
    key = f"op:{uuid.uuid4().hex}"
    a = ledger.begin(key, "kestrel", "POST", "/v2/x")
    b = ledger.begin(key, "kestrel", "POST", "/v2/x")
    assert a.request_id == b.request_id
    done = ledger.succeed(
        key,
        http_status=201,
        resource_id=f"R{uuid.uuid4().hex[:8]}",
        resource_status="COMPLETED",
        debug_id=None,
    )
    assert done.state is OpState.SUCCEEDED and done.resource_id in ledger.resource_ids()


def test_nonces_vault_budget_cache_reconcile_webhooks(engine):
    from lineage.nonces import SqlNonceRegistry
    from lineage.vault import Address, SqlAddressVault
    from llm.budget import BudgetTracker, Limits
    from paypal.webhooks import WebhookStore

    nonces = SqlNonceRegistry(engine)
    n = uuid.uuid4().hex
    assert nonces.claim("u", n) is True and nonces.claim("u", n) is False

    user = f"u-{uuid.uuid4().hex[:8]}"
    vault = SqlAddressVault(engine)
    vault.save_confirmed(
        user,
        "home",
        Address(full_name="B", address_line_1="1 Main", admin_area_2="San Jose", country_code="US"),
    )
    assert SqlAddressVault(engine).lookup(user, "home").value.admin_area_2 == "San Jose"

    model = f"m-{uuid.uuid4().hex[:8]}"
    lim = Limits(1000, 10, 10_000, requests_per_minute=5, day_reset_tz="America/Los_Angeles")
    b = BudgetTracker(engine, {model: lim}, lim)
    b.record("gemini", model, "planner", 120)
    assert b.spend(model).requests_today == 1 and b.wait_seconds(model, 10) == 0.0

    store = WebhookStore(engine)
    event = {
        "id": f"WH-{uuid.uuid4().hex}",
        "event_type": "PAYMENT.CAPTURE.REFUNDED",
        "resource": {"id": "R1"},
    }
    assert (
        store.add("kestrel", event, "tx-1") is True and store.add("kestrel", event, "tx-1") is False
    )
