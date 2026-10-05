"""Remedy execution and reconciliation against a mocked PayPal: idempotency, GET truth, polling."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from blackbox.reconcile import ReconciliationPoller
from blackbox.remedy import Action, RemedyPlan
from blackbox.remedy.execute import RemedyError, execute
from paypal.client import PayPalClient
from paypal.config import Credentials, HttpSettings
from paypal.ledger import RequestLedger


class Rails:
    """Payments v2 refunds/voids, Payouts v1 and Orders v2 GET, with scripted status changes."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any] | None]] = []
        self.status: dict[str, list[str]] = {
            "refund": ["PENDING", "COMPLETED"],
            "payout": ["PENDING", "SUCCESS"],
            "auth": ["VOIDED"],
        }
        self.order_refunds: list[dict[str, Any]] = []

    def _next(self, kind: str) -> str:
        seq = self.status[kind]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if m == "POST":
            self.posts.append((p, json.loads(req.content) if req.content else None))
        if m == "POST" and p == "/v2/payments/authorizations/A1/void":
            return httpx.Response(204)
        if m == "GET" and p == "/v2/payments/authorizations/A1":
            return httpx.Response(200, json={"id": "A1", "status": self._next("auth")})
        if m == "POST" and p == "/v2/payments/captures/C1/refund":
            return httpx.Response(201, json={"id": "R1", "status": "PENDING"})
        if m == "GET" and p == "/v2/payments/refunds/R1":
            amount = {"currency_code": "USD", "value": "45.26"}
            return httpx.Response(
                200, json={"id": "R1", "status": self._next("refund"), "amount": amount}
            )
        if m == "POST" and p == "/v1/payments/payouts":
            hdr = {"payout_batch_id": "B1", "batch_status": "PENDING"}
            return httpx.Response(201, json={"batch_header": hdr})
        if m == "GET" and p == "/v1/payments/payouts/B1":
            hdr = {"payout_batch_id": "B1", "batch_status": self._next("payout")}
            return httpx.Response(200, json={"batch_header": hdr})
        if m == "GET" and p == "/v2/checkout/orders/O1":
            pu = {"payments": {"refunds": self.order_refunds}} if self.order_refunds else {}
            return httpx.Response(200, json={"id": "O1", "purchase_units": [pu, {}]})
        return httpx.Response(404, json={"name": "NOT_FOUND"})


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    rails, ledger, clock, events = Rails(), RequestLedger(engine), Clock(), []
    settings = HttpSettings("https://pp.test", 5, 0, 0, 0, 60)

    def client(app: str) -> PayPalClient:
        return PayPalClient(
            Credentials(app, "cid-123456", "s"),
            settings,
            transport=httpx.MockTransport(rails.handler),
            ledger=ledger,
        )

    poller = ReconciliationPoller(
        engine,
        client,
        record=lambda case, t, p: events.append((case, t, p)),
        interval_seconds=5,
        now=clock,
    )
    return rails, ledger, clock, events, client, poller


def plan(*actions: Action, status: str = "proposed") -> RemedyPlan:
    return RemedyPlan(
        status=status,
        harm="wrong_item",
        r="90.52",
        actions=list(actions),
        absorbed_by_user="0.00",
        over_cap="0.00",
        notes=[],
    )


def run(env, p: RemedyPlan, **over):
    rails, ledger, clock, events, client, poller = env
    recorded: list[tuple[str, dict[str, Any]]] = []
    kw = {
        "case_id": "r-1",
        "merchant_id": "kestrel",
        "authorization_id": "A1",
        "capture_id": "C1",
        "currency": "USD",
        "buyer_email": "buyer@example.com",
        "paypal": client,
        "poller": poller,
        "record": lambda t, payload: recorded.append((t, payload)),
    }
    kw.update(over)
    return execute(p, **kw), recorded


def test_refund_and_payout_move_money_once_and_settle(env):
    rails, ledger, clock, events, client, poller = env
    p = plan(
        Action("refund", "45.26", "merchant", "misrepresentation share"),
        Action("payout", "10.00", "operator", "agent share"),
    )
    out, recorded = run(env, p)
    assert [(r["kind"], r["resource_id"], r["status"]) for r in out] == [
        ("refund", "R1", "PENDING"),
        ("payout", "B1", "PENDING"),
    ]
    assert [t for t, _ in recorded] == ["remedy.executed", "remedy.executed"]
    refund_body = rails.posts[0][1]
    assert refund_body["amount"] == {"currency_code": "USD", "value": "45.26"}
    assert "r-1" in refund_body["note_to_payer"]
    payout = rails.posts[1][1]
    assert payout["items"][0]["receiver"] == "buyer@example.com"
    assert payout["sender_batch_header"]["sender_batch_id"] == "pv-r-1"

    # Approval clicked twice: nothing is re-sent, ids come from the ledger.
    again, _ = run(env, p)
    assert len(rails.posts) == 2
    assert [r["resource_id"] for r in again] == ["R1", "B1"]
    assert ledger.resource_ids() >= {"R1", "B1"}

    assert sorted(poller.pending("r-1")) == ["B1", "R1"]
    assert poller.settle("r-1", max_rounds=5, sleep=clock.sleep)
    finals = [(p["resource_id"], p["to"]) for _, t, p in events if t == "reconcile.observed"]
    assert ("R1", "COMPLETED") in finals and ("B1", "SUCCESS") in finals
    assert poller.pending("r-1") == []


def test_void_is_idempotent_and_tracked(env):
    rails, ledger, clock, events, client, poller = env
    p = plan(Action("void", "90.52", "merchant", "not captured"))
    out, _ = run(env, p, capture_id=None)
    assert out == [{"kind": "void", "resource_id": "A1", "status": "VOIDED"}]
    run(env, p, capture_id=None)
    assert [x for x, _ in rails.posts] == ["/v2/payments/authorizations/A1/void"]
    assert poller.settle("r-1", max_rounds=1, sleep=clock.sleep)


@pytest.mark.parametrize(
    ("p", "over", "msg"),
    [
        (plan(status="needs_human_review"), {}, "needs_human_review"),
        (
            plan(Action("void", "1.00", "merchant", "x")),
            {"authorization_id": None},
            "no authorization",
        ),
        (plan(Action("refund", "1.00", "merchant", "x")), {"capture_id": None}, "no capture"),
        (plan(Action("teleport", "1.00", "merchant", "x")), {}, "unknown action"),
    ],
)
def test_refuses_what_it_cannot_do(env, p, over, msg):
    with pytest.raises(RemedyError, match=msg):
        run(env, p, **over)


def test_poller_waits_for_its_interval_hints_and_gives_up(env):
    rails, ledger, clock, events, client, poller = env
    rails.status["refund"] = ["PENDING"]  # never completes
    poller.track("refund", "R1", "kestrel", "r-2")
    assert [o.new for o in poller.poll_due()] == ["PENDING"]
    assert poller.poll_due() == []  # not due yet, and no change is recorded twice
    poller.track("refund", "R1", "kestrel", "r-2")  # tracking again = hint
    assert poller.poll_due() == []  # due, polled, status unchanged so nothing new observed
    assert not poller.settle("r-2", max_rounds=2, sleep=clock.sleep)
    with pytest.raises(ValueError, match="unknown resource kind"):
        poller.track("parcel", "X", "kestrel", "r-2")


def test_discrepancies_report_refunds_we_did_not_cause(env):
    rails, ledger, clock, events, client, poller = env
    assert poller.discrepancies(case_id="r-3", order_id="O1", app="kestrel", caused=set()) == []
    rails.order_refunds = [
        {"id": "R1", "status": "COMPLETED", "amount": {"value": "45.26"}},
        {"id": "RX", "status": "FAILED"},
    ]
    out = poller.discrepancies(case_id="r-3", order_id="O1", app="kestrel", caused={"R1"})
    assert out == [{"refund_id": "RX", "status": "FAILED", "amount": None}]
    assert events[-1][1] == "reconcile.discrepancy"
