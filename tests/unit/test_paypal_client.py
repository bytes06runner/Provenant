"""Unit tests for the PayPal client using an in-process transport (no network)."""

from __future__ import annotations

import httpx
import pytest

from paypal.client import PayPalClient, PayPalError
from paypal.config import Credentials, HttpSettings
from paypal.redact import redact

SETTINGS = HttpSettings(
    api_base="https://paypal.test",
    timeout_seconds=5,
    max_retries=2,
    backoff_base_seconds=0.5,
    backoff_max_seconds=8,
    token_refresh_margin_seconds=60,
)
CREDS = Credentials(label="t", client_id="cid-123456", client_secret="shh")


class Recorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return self.responses.pop(0)


def make(responses: list[httpx.Response], now: list[float] | None = None):
    rec = Recorder(responses)
    sleeps: list[float] = []
    clock = now if now is not None else [0.0]
    client = PayPalClient(
        CREDS,
        SETTINGS,
        transport=httpx.MockTransport(rec),
        sleep=sleeps.append,
        clock=lambda: clock[0],
    )
    return client, rec, sleeps, clock


def api_calls(rec: Recorder) -> list[httpx.Request]:
    return [r for r in rec.requests if r.url.path != "/v1/oauth2/token"]


def test_token_cached_until_margin_then_refreshed():
    client, rec, _, clock = make([httpx.Response(200, json={})] * 3)
    client.get("/a")
    client.get("/b")
    assert sum(r.url.path == "/v1/oauth2/token" for r in rec.requests) == 1
    clock[0] = 3600 - 60 + 1  # past expires_in minus margin
    client.get("/c")
    assert sum(r.url.path == "/v1/oauth2/token" for r in rec.requests) == 2


def test_post_sets_request_id_and_reuses_it_across_retries():
    client, rec, sleeps, _ = make(
        [httpx.Response(503), httpx.Response(500), httpx.Response(201, json={"id": "X"})]
    )
    resp = client.post("/v2/checkout/orders", json={"a": 1})
    ids = {r.headers["PayPal-Request-Id"] for r in api_calls(rec)}
    assert resp.status_code == 201
    assert len(api_calls(rec)) == 3
    assert ids == {resp.request_id}
    assert sleeps == [0.5, 1.0]


def test_pinned_request_id_is_used():
    client, rec, _, _ = make([httpx.Response(201, json={"id": "R"})])
    client.post("/v2/payments/captures/C/refund", json={}, request_id="fixed-id")
    assert api_calls(rec)[0].headers["PayPal-Request-Id"] == "fixed-id"


def test_get_has_no_request_id():
    client, rec, _, _ = make([httpx.Response(200, json={})])
    client.get("/v2/checkout/orders/1")
    assert "PayPal-Request-Id" not in api_calls(rec)[0].headers


def test_no_retry_on_4xx_and_structured_error():
    body = {
        "name": "UNPROCESSABLE_ENTITY",
        "message": "The requested action could not be performed",
        "debug_id": "dbg1",
        "details": [{"issue": "ORDER_NOT_APPROVED"}],
    }
    client, rec, sleeps, _ = make([httpx.Response(422, json=body)])
    with pytest.raises(PayPalError) as ei:
        client.post("/v2/checkout/orders/1/authorize")
    assert ei.value.issues == ["ORDER_NOT_APPROVED"]
    assert ei.value.debug_id == "dbg1"
    assert len(api_calls(rec)) == 1
    assert sleeps == []


def test_gives_up_after_max_retries():
    client, rec, _, _ = make([httpx.Response(500)] * 3)
    with pytest.raises(PayPalError):
        client.get("/x")
    assert len(api_calls(rec)) == 1 + SETTINGS.max_retries


def test_401_refreshes_token_once():
    client, rec, _, _ = make([httpx.Response(401), httpx.Response(200, json={"ok": True})])
    assert client.get("/x").body == {"ok": True}
    assert sum(r.url.path == "/v1/oauth2/token" for r in rec.requests) == 2


def test_credentials_repr_hides_secret():
    assert "shh" not in repr(CREDS)


def test_redact_masks_tokens_emails_and_names():
    out = redact(
        {
            "access_token": "abc",
            "payer": {
                "email_address": "buyer@personal.example.com",
                "name": {"given_name": "Jane", "surname": "Doe"},
            },
            "links": [{"href": "mailto:someone@example.com"}],
        }
    )
    assert out["access_token"] == "[REDACTED]"
    assert out["payer"]["email_address"] == "b***@personal.example.com"
    assert out["payer"]["name"] == {"given_name": "J***", "surname": "D***"}
    assert out["links"][0]["href"] == "mailto:s***@example.com"
