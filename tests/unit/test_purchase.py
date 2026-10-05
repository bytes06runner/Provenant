"""Purchase session end to end with fakes: scripted LLMs, fake tools, mocked PayPal."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from blackbox.recorder import CustomIdFormat, FlightRecorder
from lineage.labels import untrusted
from lineage.mandate import MandateError
from lineage.nonces import SqlNonceRegistry
from lineage.purchase import PurchaseError, PurchaseSession
from lineage.signing import key_id
from paypal.client import PayPalClient
from paypal.config import Credentials, HttpSettings

from .lineage_world import NOW, USER, World
from .test_dsl_interpreter import FakeTools
from .test_roles_toolbox import GOOD_PLAN, ScriptedRouter, mandate_json

FMT = CustomIdFormat("pv:", 32, 127)
SETTINGS = {
    "mandate_ttl_minutes": 60,
    "planner_prompt": "planner_v2",
    "planner_max_repairs": 1,
    "approval_poll_seconds": 0,
    "approval_timeout_seconds": 5,
}
SPEC = {
    "category": "trail-running-shoes",
    "currency": "USD",
    "attributes": {"color": ["black", "navy"], "size_us": ["10"], "material": ["mesh", "leather"]},
}


class FakePayPal:
    """Just enough of Orders v2 and Payments v2 to drive the session."""

    def __init__(
        self,
        *,
        approve_after: int = 0,
        drop_custom_id: bool = False,
        auth_custom_id: str | None = None,
    ) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.polls = 0
        self.approve_after = approve_after
        self.drop_custom_id = drop_custom_id
        self.auth_custom_id = auth_custom_id

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.method == "POST" and path == "/v2/checkout/orders":
            body = json.loads(req.content)
            if self.drop_custom_id:
                body["purchase_units"][0].pop("custom_id")
            self.orders["O1"] = body
            return httpx.Response(201, json={"id": "O1", "status": "PAYER_ACTION_REQUIRED"})
        if req.method == "GET" and path == "/v2/checkout/orders/O1":
            self.polls += 1
            status = "APPROVED" if self.polls > self.approve_after + 1 else "PAYER_ACTION_REQUIRED"
            return httpx.Response(
                200,
                json={
                    "id": "O1",
                    "status": status,
                    "purchase_units": self.orders["O1"]["purchase_units"],
                    "links": [{"rel": "payer-action", "href": "https://pp/checkoutnow?token=O1"}],
                },
            )
        if req.method == "POST" and path == "/v2/checkout/orders/O1/authorize":
            return httpx.Response(
                201,
                json={
                    "id": "O1",
                    "purchase_units": [
                        {"payments": {"authorizations": [{"id": "A1", "status": "CREATED"}]}}
                    ],
                },
            )
        if req.method == "GET" and path == "/v2/payments/authorizations/A1":
            cid = self.auth_custom_id or self.orders["O1"]["purchase_units"][0].get("custom_id")
            return httpx.Response(
                200,
                json={
                    "id": "A1",
                    "status": "CREATED",
                    "custom_id": cid,
                    "expiration_time": "2026-11-03T00:00:00Z",
                },
            )
        return httpx.Response(404)


def session(
    world: World,
    router: ScriptedRouter,
    paypal: FakePayPal | None = None,
    tools: FakeTools | None = None,
) -> PurchaseSession:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    paypal = paypal or FakePayPal()
    settings = HttpSettings("https://pp.test", 5, 0, 0, 0, 60)
    return PurchaseSession(
        session_id="s-test",
        recorder=FlightRecorder(engine),
        router=router,
        toolbox=tools or FakeTools(world, merchants=("northwind",)),
        vault=world.vault,
        user_id=USER,
        user_key=world.user_key,
        user_keys=world.user_keys,
        nonces=SqlNonceRegistry(engine),
        custom_id_format=FMT,
        paypal=lambda mid: PayPalClient(
            Credentials(mid, "cid-123456", "s"),
            settings,
            transport=httpx.MockTransport(paypal.handler),
        ),
        settings=SETTINGS,
        category_spec=SPEC,
        now=lambda: NOW,
        experience_context={"user_action": "CONTINUE"},
    )


def full_flow(world, paypal=None, tools=None):
    router = ScriptedRouter({"mandate": [mandate_json()], "planner": [json.dumps(GOOD_PLAN)]})
    s = session(world, router, paypal, tools)
    proposal = s.propose("black trail shoes, size 10, no leather", ["home"])
    vm = s.confirm_and_sign(proposal.fields)
    plan = s.plan(vm, review_url=None)
    p = s.run(vm, plan)
    return s, vm, p


@pytest.fixture
def world() -> World:
    return World()


def test_request_to_paypal_order_with_custom_id_bound_to_the_chain(world):
    paypal = FakePayPal()
    s, vm, p = full_flow(world, paypal)
    assert p.result.allowed
    order = s.create_order(p)
    sent = paypal.orders["O1"]["purchase_units"][0]
    assert sent["custom_id"] == order.custom_id and sent["payee"] == {"merchant_id": "NWPAYEE123"}
    assert order.approval_url == "https://pp/checkoutnow?token=O1"
    bound = s.recorder.resolve_custom_id("s-test", order.custom_id, FMT)
    assert bound.seq == order.bound_event_seq
    kinds = [e.event_type for e in s.recorder.events("s-test")]
    for k in (
        "request",
        "mandate.proposed",
        "mandate.signed",
        "plan.generated",
        "contract.precheck",
        "contract.final",
        "checkout.order_request",
        "paypal.order.created",
    ):
        assert k in kinds, k
    s.recorder.verify("s-test")


def test_signed_mandate_is_user_labeled_and_single_use(world):
    s, vm, _ = full_flow(world)
    assert vm.mandate.user_id == USER and vm.envelope.key_id == key_id(world.user_key.public_key())
    with pytest.raises(MandateError, match="replay"):
        from lineage.mandate import verify_mandate

        verify_mandate(
            vm.envelope, user_id=USER, user_keys=world.user_keys, now=NOW, nonces=s.nonces
        )


def test_planner_context_has_no_address_or_user_identity(world):
    router = ScriptedRouter({"mandate": [mandate_json()], "planner": [json.dumps(GOOD_PLAN)]})
    s = session(world, router)
    vm = s.confirm_and_sign(s.propose("x", ["home"]).fields)
    s.plan(vm, review_url="https://shop/reviews/1")
    _, req, _ = router.requests[-1]
    context = json.loads(req.messages[1].content)
    assert set(context) == {"mandate", "review_url"}
    assert "ship_to_ref" not in context["mandate"] and "user_id" not in context["mandate"]


def test_hijacks_are_blocked_recorded_and_never_built(world):
    s, vm, p = full_flow(world)
    url = "page:https://atk/reviews/1"
    injected = dataclasses.replace(p.candidate.checkout, payee=untrusted("ATKPAYEE666", url))
    r1 = s.check_hijack(vm, p, injected, "payee from page")
    signed = dataclasses.replace(p.candidate.checkout, payee=world.manifest("attacker").payee())
    r2 = s.check_hijack(vm, p, signed, "attacker signed payee")
    assert not r1.allowed and not r2.allowed
    blocked = [e.payload for e in s.recorder.events("s-test") if e.event_type == "contract.blocked"]
    assert [b["attack"] for b in blocked] == ["payee from page", "attacker signed payee"]
    assert all(b["order_builder"].startswith("refused") for b in blocked)
    # Regression: UNTRUSTED is the zero value of the Label enum; it must still be recorded.
    assert blocked[0]["violations"][0]["label"] == "UNTRUSTED"
    assert any(url in x for x in blocked[0]["violations"][0]["provenance"])


def test_an_allowed_variant_is_recorded_as_allowed(world):
    s, vm, p = full_flow(world)
    r = s.check_hijack(vm, p, p.candidate.checkout, "no change")
    assert r.allowed
    assert s.recorder.events("s-test")[-1].event_type == "contract.allowed"


def test_authorize_after_approval_keeps_the_binding(world):
    paypal = FakePayPal(approve_after=2)
    s, _, p = full_flow(world, paypal)
    order = s.create_order(p)
    auth = s.authorize_when_approved(order)
    assert auth["authorization_id"] == "A1" and auth["custom_id"] == order.custom_id
    assert s.recorder.events("s-test")[-1].event_type == "paypal.authorization"


def test_lost_custom_id_is_detected(world):
    s, _, p = full_flow(world, FakePayPal(drop_custom_id=True))
    with pytest.raises(PurchaseError, match="did not store the custom_id"):
        s.create_order(p)


def test_authorization_with_wrong_custom_id_is_detected(world):
    s, _, p = full_flow(world, FakePayPal(auth_custom_id="pv:" + "f" * 32))
    order = s.create_order(p)
    with pytest.raises(PurchaseError, match="lost the custom_id"):
        s.authorize_when_approved(order)


def test_approval_timeout(world):
    paypal = FakePayPal(approve_after=10**6)
    s, _, p = full_flow(world, paypal)
    s.settings = {**SETTINGS, "approval_timeout_seconds": 0}
    order = s.create_order(p)
    with pytest.raises(PurchaseError, match="not approved"):
        s.authorize_when_approved(order)


def test_blocked_final_result_never_creates_an_order(world):
    s, _, p = full_flow(world)
    from lineage.contracts import ContractResult, Field, Rule, Violation

    blocked = dataclasses.replace(
        p, result=ContractResult((Violation(Field.PAYEE, Rule.UNTRUSTED, "x"),))
    )
    with pytest.raises(PurchaseError, match="did not allow"):
        s.create_order(blocked)
