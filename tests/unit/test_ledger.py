"""Request ledger: idempotency is enforced by our records, not by PayPal's response codes."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from paypal.client import AlreadyCompleted, PayPalClient, PayPalError
from paypal.config import Credentials, HttpSettings
from paypal.ledger import LedgerConflict, OpState, RequestLedger

SETTINGS = HttpSettings(
    api_base="https://paypal.test",
    timeout_seconds=5,
    max_retries=0,
    backoff_base_seconds=0,
    backoff_max_seconds=0,
    token_refresh_margin_seconds=60,
)
CREDS = Credentials(label="merchant:test", client_id="cid-123456", client_secret="shh")


@pytest.fixture
def ledger() -> RequestLedger:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    return RequestLedger(engine)


def client_with(ledger: RequestLedger, handler):
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append(request)
        return handler(request)

    return PayPalClient(
        CREDS, SETTINGS, transport=httpx.MockTransport(transport), ledger=ledger
    ), seen


def test_begin_is_stable_per_operation(ledger):
    a = ledger.begin("refund:cap1:r1", "merchant:test", "POST", "/v2/payments/captures/cap1/refund")
    b = ledger.begin("refund:cap1:r1", "merchant:test", "POST", "/v2/payments/captures/cap1/refund")
    assert a.request_id == b.request_id
    assert a.state is OpState.PENDING


def test_operation_key_cannot_be_reused_for_another_call(ledger):
    ledger.begin("op", "merchant:test", "POST", "/a")
    with pytest.raises(LedgerConflict):
        ledger.begin("op", "merchant:test", "POST", "/b")


def test_success_records_request_id_and_resource(ledger):
    client, seen = client_with(
        ledger, lambda r: httpx.Response(201, json={"id": "R1", "status": "COMPLETED"})
    )
    resp = client.post("/v2/payments/captures/C/refund", json={}, operation_key="refund:C:1")
    entry = ledger.get("refund:C:1")
    assert entry.state is OpState.SUCCEEDED
    assert entry.request_id == seen[0].headers["PayPal-Request-Id"] == resp.request_id
    assert (entry.resource_id, entry.resource_status) == ("R1", "COMPLETED")


def test_succeeded_operation_is_never_resent(ledger):
    client, seen = client_with(
        ledger, lambda r: httpx.Response(200, json={"id": "R1", "status": "COMPLETED"})
    )
    client.post("/v2/payments/captures/C/refund", json={}, operation_key="refund:C:1")
    with pytest.raises(AlreadyCompleted) as done:
        client.post("/v2/payments/captures/C/refund", json={}, operation_key="refund:C:1")
    assert done.value.entry.resource_id == "R1"
    assert len(seen) == 1


def test_failure_is_recorded_and_retry_reuses_request_id(ledger):
    responses = [
        httpx.Response(
            422,
            json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "TRANSACTION_REFUSED"}]},
        ),
        httpx.Response(201, json={"id": "R2", "status": "COMPLETED"}),
    ]
    client, seen = client_with(ledger, lambda r: responses.pop(0))
    with pytest.raises(PayPalError):
        client.post("/x", json={}, operation_key="op")
    failed = ledger.get("op")
    assert failed.state is OpState.FAILED and failed.resource_id is None
    assert "TRANSACTION_REFUSED" in failed.error
    client.post("/x", json={}, operation_key="op")
    ok = ledger.get("op")
    assert ok.state is OpState.SUCCEEDED and ok.resource_id == "R2" and ok.attempts == 2
    assert seen[0].headers["PayPal-Request-Id"] == seen[1].headers["PayPal-Request-Id"]


def test_transport_failure_leaves_pending_with_same_request_id(ledger):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(201, json={"id": "R3"})

    client, _ = client_with(ledger, flaky)
    with pytest.raises(httpx.ConnectError):
        client.post("/x", json={}, operation_key="op")
    pending = ledger.get("op")
    assert pending.state is OpState.PENDING
    resp = client.post("/x", json={}, operation_key="op")
    assert resp.request_id == pending.request_id


def test_payout_batch_resource_is_recorded(ledger):
    body = {"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}}
    client, _ = client_with(ledger, lambda r: httpx.Response(201, json=body))
    client.post("/v1/payments/payouts", json={}, operation_key="payout:1")
    entry = ledger.get("payout:1")
    assert (entry.resource_id, entry.resource_status) == ("B1", "PENDING")


def test_every_post_is_persisted_even_without_operation_key(ledger):
    client, seen = client_with(ledger, lambda r: httpx.Response(201, json={"id": "O1"}))
    client.post("/v2/checkout/orders", json={})
    with ledger.engine.connect() as conn:
        rows = conn.exec_driver_sql("select request_id, resource_id from paypal_requests").all()
    assert rows == [(seen[0].headers["PayPal-Request-Id"], "O1")]


def test_pinned_request_id_rejected_with_ledger(ledger):
    client, _ = client_with(ledger, lambda r: httpx.Response(201, json={}))
    with pytest.raises(ValueError):
        client.post("/x", json={}, request_id="mine")


def test_empty_error_body_still_recorded_meaningfully(ledger):
    client, _ = client_with(ledger, lambda r: httpx.Response(403))
    with pytest.raises(PayPalError):
        client.post("/x", json={}, operation_key="op")
    assert ledger.get("op").error == "HTTP 403, empty body"


def test_resource_ids_lists_what_we_caused(ledger):
    client, _ = client_with(ledger, lambda r: httpx.Response(201, json={"id": "R9"}))
    client.post("/x", json={}, operation_key="a")
    ledger.begin("pending-op", "merchant:test", "POST", "/y")
    assert ledger.resource_ids() == {"R9"}
