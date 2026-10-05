"""Fulfillment and capture of a recorded purchase, and waiting for the buyer's approval."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from blackbox.recorder import FlightRecorder
from lineage.fulfillment import FulfillmentError, authorize_recorded, fulfill_and_capture
from paypal.client import PayPalClient
from paypal.config import Credentials, HttpSettings
from paypal.ledger import RequestLedger


class PayPal:
    def __init__(
        self, *, approve_after: int = 0, track_status: int = 201, custom_id: str = "pv:abc"
    ):
        self.approve_after = approve_after
        self.track_status = track_status
        self.custom_id = custom_id
        self.gets = 0
        self.authorized = False
        self.posts: list[tuple[str, Any]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if m == "POST":
            self.posts.append((p, json.loads(req.content) if req.content else None))
        if m == "GET" and p == "/v2/checkout/orders/O1":
            self.gets += 1
            if self.authorized:
                pu = [{"payments": {"authorizations": [{"id": "A1", "status": "CREATED"}]}}]
                return httpx.Response(
                    200, json={"id": "O1", "status": "COMPLETED", "purchase_units": pu}
                )
            status = "APPROVED" if self.gets > self.approve_after else "PAYER_ACTION_REQUIRED"
            return httpx.Response(200, json={"id": "O1", "status": status})
        if m == "POST" and p == "/v2/checkout/orders/O1/authorize":
            self.authorized = True
            return httpx.Response(201, json={"id": "O1", "status": "COMPLETED"})
        if m == "GET" and p == "/v2/payments/authorizations/A1":
            return httpx.Response(
                200, json={"id": "A1", "status": "CREATED", "custom_id": self.custom_id}
            )
        if m == "POST" and p == "/v2/payments/authorizations/A1/capture":
            return httpx.Response(201, json={"id": "C1", "status": "COMPLETED"})
        if m == "GET" and p == "/v2/payments/captures/C1":
            amount = {"currency_code": "USD", "value": "104.00"}
            return httpx.Response(
                200,
                json={"id": "C1", "status": "COMPLETED", "amount": amount, "custom_id": "pv:abc"},
            )
        if m == "POST" and p == "/v2/checkout/orders/O1/track":
            if self.track_status >= 400:
                return httpx.Response(
                    self.track_status, json={"name": "UNPROCESSABLE_ENTITY", "message": "no"}
                )
            return httpx.Response(self.track_status, json={"id": "O1"})
        return httpx.Response(404, json={"name": "NOT_FOUND"})


def storefront(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content)
    wrong = body["force_wrong_variant"]
    return httpx.Response(
        200,
        json={
            "order_ref": body["order_ref"],
            "shipped_sku": "NOR-004" if wrong else body["sku"],
            "shipped_attributes": {"color": "navy" if wrong else "black"},
            "tracking_number": "SIM1",
        },
    )


@pytest.fixture
def rec() -> FlightRecorder:
    return FlightRecorder(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    )


def clients(pp: PayPal, rec: FlightRecorder):
    ledger = RequestLedger(rec.engine)
    settings = HttpSettings("https://pp.test", 5, 0, 0, 0, 60)
    return lambda mid: PayPalClient(
        Credentials(mid, "cid-123456", "s"),
        settings,
        transport=httpx.MockTransport(pp.handler),
        ledger=ledger,
    )


def order(rec: FlightRecorder, *, authorized: bool) -> None:
    rec.append(
        "s-1", "plan.proposed", {"candidate": {"merchant_id": "northwind", "sku": "NOR-001"}}
    )
    pu = [{"custom_id": "pv:abc"}]
    rec.append(
        "s-1",
        "paypal.order.created",
        {"merchant_id": "northwind", "order": {"id": "O1", "purchase_units": pu}},
    )
    if authorized:
        rec.append("s-1", "paypal.authorization", {"authorization_id": "A1"})


def fulfill(rec, pp, wrong=False):
    return fulfill_and_capture(
        recorder=rec,
        session_id="s-1",
        storefront_url="http://shop.test",
        http=httpx.Client(transport=httpx.MockTransport(storefront)),
        paypal=clients(pp, rec),
        force_wrong_variant=wrong,
    )


@pytest.mark.parametrize("wrong", [False, True])
def test_ship_capture_and_track_are_recorded(rec, wrong):
    order(rec, authorized=True)
    pp = PayPal()
    out = fulfill(rec, pp, wrong)
    assert out["capture_id"] == "C1" and out["tracking"] == {"ok": True, "status_code": 201}
    assert out["shipment"]["shipped_sku"] == ("NOR-004" if wrong else "NOR-001")
    kinds = [e.event_type for e in rec.events("s-1")]
    assert kinds[-3:] == ["fulfillment.shipped", "paypal.capture", "paypal.tracking"]
    assert rec.events("s-1")[-3].payload["planted"] is wrong
    track = pp.posts[-1][1]
    assert track["tracking_number"] == "SIM1" and track["capture_id"] == "C1"


def test_tracking_failure_is_recorded_not_fatal(rec):
    order(rec, authorized=True)
    out = fulfill(rec, PayPal(track_status=422))
    assert out["tracking"]["ok"] is False and "UNPROCESSABLE" in out["tracking"]["error"]


def test_cannot_fulfill_before_authorization(rec):
    order(rec, authorized=False)
    with pytest.raises(FulfillmentError, match="authorized"):
        fulfill(rec, PayPal())


def authorize(rec, pp, timeout=10.0):
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s

    return authorize_recorded(
        recorder=rec,
        session_id="s-1",
        paypal=clients(pp, rec),
        poll_seconds=1,
        timeout_seconds=timeout,
        sleep=sleep,
        clock=lambda: t[0],
    )


def test_waits_for_approval_then_authorizes_once(rec):
    order(rec, authorized=False)
    pp = PayPal(approve_after=2)
    out = authorize(rec, pp)
    assert out["authorization_id"] == "A1" and out["custom_id"] == "pv:abc"
    assert authorize(rec, pp)["authorization_id"] == "A1"  # already COMPLETED: no second POST
    assert [p for p, _ in pp.posts] == ["/v2/checkout/orders/O1/authorize"]
    assert [e.event_type for e in rec.events("s-1")].count("paypal.authorization") == 1


def test_gives_up_when_never_approved(rec):
    order(rec, authorized=False)
    with pytest.raises(FulfillmentError, match="PAYER_ACTION_REQUIRED"):
        authorize(rec, PayPal(approve_after=99), timeout=3)


def test_lost_custom_id_binding_is_an_error(rec):
    order(rec, authorized=False)
    with pytest.raises(FulfillmentError, match="custom_id"):
        authorize(rec, PayPal(custom_id="pv:other"))
