"""Webhook intake: only verified events are stored, and each event is stored once."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from paypal.client import PayPalClient
from paypal.config import Credentials, HttpSettings
from paypal.webhooks import Outcome, WebhookStore, handle_delivery

SETTINGS = HttpSettings("https://paypal.test", 5, 0, 0, 0, 60)
CREDS = Credentials(label="merchant:test", client_id="cid-123456", client_secret="shh")
HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "PAYPAL-TRANSMISSION-ID": "tx-1",
    "PAYPAL-TRANSMISSION-SIG": "sig",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-04T15:00:00Z",
}
EVENT = {
    "id": "WH-1",
    "event_type": "PAYMENT.AUTHORIZATION.VOIDED",
    "resource_type": "authorization",
    "resource": {"id": "AUTH-1", "status": "VOIDED"},
}


@pytest.fixture
def store() -> WebhookStore:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    return WebhookStore(engine)


def verifier(status: str | int, seen: list[dict] | None = None) -> PayPalClient:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if seen is not None:
            seen.append(json.loads(request.content))
        if isinstance(status, int):
            return httpx.Response(status, json={"name": "INTERNAL_SERVER_ERROR"})
        return httpx.Response(200, json={"verification_status": status})

    return PayPalClient(CREDS, SETTINGS, transport=httpx.MockTransport(transport))


def deliver(store, client, headers=HEADERS, body=EVENT):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return handle_delivery(
        app_label="merchant:test",
        webhook_id="WHID-1",
        headers=headers,
        raw_body=raw,
        verifier=client,
        store=store,
    )


def test_verified_event_is_stored(store):
    seen: list[dict] = []
    result = deliver(store, verifier("SUCCESS", seen))
    assert result.outcome is Outcome.ACCEPTED and result.http_status == 200
    rows = store.events()
    assert [(r["event_id"], r["resource_id"]) for r in rows] == [("WH-1", "AUTH-1")]
    sent = seen[0]
    assert sent["webhook_id"] == "WHID-1"
    assert sent["transmission_id"] == "tx-1"
    assert sent["webhook_event"] == EVENT


def test_duplicate_delivery_is_acknowledged_and_not_stored_twice(store):
    client = verifier("SUCCESS")
    assert deliver(store, client).outcome is Outcome.ACCEPTED
    again = deliver(store, client)
    assert again.outcome is Outcome.DUPLICATE and again.http_status == 200
    assert len(store.events()) == 1


def test_forged_signature_is_rejected_and_not_stored(store):
    result = deliver(store, verifier("FAILURE"))
    assert result.outcome is Outcome.REJECTED and result.http_status == 400
    assert store.events() == []


def test_forged_event_reusing_a_real_id_is_rejected(store):
    assert deliver(store, verifier("SUCCESS")).outcome is Outcome.ACCEPTED
    forged = {**EVENT, "resource": {"id": "AUTH-1", "status": "CAPTURED"}}
    result = deliver(store, verifier("FAILURE"), body=forged)
    assert result.outcome is Outcome.REJECTED
    assert json.loads(store.events()[0]["raw_json"])["resource"]["status"] == "VOIDED"


@pytest.mark.parametrize("header", list(HEADERS))
def test_missing_transmission_header_rejected_without_calling_paypal(store, header):
    seen: list[dict] = []
    headers = {k: v for k, v in HEADERS.items() if k != header}
    result = deliver(store, verifier("SUCCESS", seen), headers=headers)
    assert result.outcome is Outcome.REJECTED and seen == []


@pytest.mark.parametrize("body", [b"not json", b"[]", json.dumps({"event_type": "X"}).encode()])
def test_malformed_body_rejected(store, body):
    assert deliver(store, verifier("SUCCESS"), body=body).outcome is Outcome.REJECTED


def test_verification_outage_asks_paypal_to_retry(store):
    result = deliver(store, verifier(500))
    assert result.outcome is Outcome.REJECTED and result.http_status == 503
    assert store.events() == []


def test_payout_batch_resource_id(store):
    event = {
        "id": "WH-2",
        "event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
        "resource": {"batch_header": {"payout_batch_id": "B1"}},
    }
    deliver(store, verifier("SUCCESS"), body=event)
    assert store.events("PAYMENT.PAYOUTSBATCH.SUCCESS")[0]["resource_id"] == "B1"
