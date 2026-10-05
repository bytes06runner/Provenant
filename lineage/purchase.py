"""One purchase session, end to end, with every step in the Flight Recorder.

    request text
      -> propose_mandate (LLM, user's words only)      -> user confirms, mandate signed (Ed25519)
      -> make_plan (planner LLM, mandate only)          -> plan validated
      -> interpreter (labels, precheck, Q-LLM on pages) -> proposal re-checked by every contract
      -> checkout builder (sealed result only)          -> PayPal order with custom_id = chain head
      -> buyer approves in PayPal                       -> authorize, verified with GET

`check_hijack` evaluates a tampered checkout (for example an attacker's payee) against the same
contracts and records the block with its provenance, so a refused attack is as auditable as a
completed purchase.
"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from blackbox.recorder import CustomIdFormat, FlightRecorder
from lineage.checkout import CheckoutError, build_order_request
from lineage.contracts import ContractResult, ProposedCheckout, check_checkout
from lineage.dsl import Plan
from lineage.interpreter import Interpreter, Proposal, Toolbox
from lineage.mandate import (
    AutonomyMode,
    IntentMandate,
    NonceRegistry,
    VerifiedMandate,
    sign_mandate,
    verify_mandate,
)
from lineage.vault import AddressVault
from llm import roles
from paypal.client import PayPalClient
from paypal.redact import redact


class PurchaseError(Exception):
    pass


@dataclass(frozen=True)
class OrderResult:
    merchant_id: str
    order_id: str
    status: str
    approval_url: str | None
    custom_id: str
    invoice_id: str
    bound_event_seq: int


@dataclass
class PurchaseSession:
    session_id: str
    recorder: FlightRecorder
    router: roles.Router
    toolbox: Toolbox
    vault: AddressVault
    user_id: str
    user_key: Ed25519PrivateKey
    user_keys: dict[str, Ed25519PublicKey]
    nonces: NonceRegistry
    custom_id_format: CustomIdFormat
    paypal: Callable[[str], PayPalClient]  # merchant_id -> client with that merchant's credentials
    settings: dict[str, Any]
    category_spec: dict[str, Any]
    now: Callable[[], datetime]
    experience_context: dict[str, Any] = field(default_factory=dict)

    # ---- recording ------------------------------------------------------------------

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        self.recorder.append(self.session_id, event_type, payload)

    # ---- 1. mandate ------------------------------------------------------------------

    def propose(self, request_text: str, address_refs: list[str]) -> roles.MandateProposal:
        self.record("request", {"user_id": self.user_id, "text": request_text})
        # Which agent policy ran, so Blackbox replays the same agent for coalitions without A.
        self.record(
            "agent.policy",
            {
                "planner_role": "planner",
                "planner_prompt": str(self.settings["planner_prompt"]),
                "rank_prompt": str(getattr(self.toolbox, "rank_prompt", "rank_v1")),
            },
        )
        proposal = roles.propose_mandate(
            self.router,
            request_text,
            vocabulary=self.category_spec["attributes"],
            category=self.category_spec["category"],
            address_refs=address_refs,
            merchants=self.toolbox.list_merchants(),
            currency=self.category_spec["currency"],
        )
        self.record(
            "mandate.proposed",
            {
                "fields": proposal.fields,
                "questions": proposal.questions,
                "problems": proposal.problems,
            },
        )
        return proposal

    def confirm_and_sign(self, fields: dict[str, Any]) -> VerifiedMandate:
        """The user has reviewed (and possibly edited) the fields. Sign and verify the mandate."""
        now = self.now()
        mandate = IntentMandate(
            mandate_id=f"m-{uuid.uuid4().hex[:16]}",
            user_id=self.user_id,
            nonce=secrets.token_hex(16),
            issued_at=now,
            expires_at=now + timedelta(minutes=int(self.settings["mandate_ttl_minutes"])),
            autonomy_mode=AutonomyMode.HUMAN_PRESENT,
            **fields,
        )
        envelope = sign_mandate(mandate, self.user_key)
        verified = verify_mandate(
            envelope, user_id=self.user_id, user_keys=self.user_keys, now=now, nonces=self.nonces
        )
        self.record("mandate.signed", {"envelope": envelope.to_dict()})
        return verified

    # ---- 2. plan and run --------------------------------------------------------------

    def plan(self, vm: VerifiedMandate, review_url: str | None = None) -> Plan:
        m = vm.mandate
        context: dict[str, Any] = {
            "mandate": {
                "category": m.category,
                "required_attributes": m.required_attributes,
                "forbidden_attributes": m.forbidden_attributes,
                "max_unit_price": m.max_unit_price,
                "max_total": m.max_total,
                "currency": m.currency,
                "quantity": m.quantity,
                "merchant_allowlist": m.merchant_allowlist,
            }
        }
        if review_url:
            context["review_url"] = review_url
        result = roles.make_plan(
            self.router,
            context,
            prompt_name=str(self.settings["planner_prompt"]),
            max_repairs=int(self.settings["planner_max_repairs"]),
        )
        self.record(
            "plan.generated",
            {
                "plan_hash": result.plan.plan_hash,
                "plan": result.plan.document,
                "attempts": result.attempts,
            },
        )
        return result.plan

    def run(self, vm: VerifiedMandate, plan: Plan) -> Proposal:
        interpreter = Interpreter(
            toolbox=self.toolbox,
            mandate=vm,
            vault=self.vault,
            now=self.now(),
            record=self.record,
        )
        proposal = interpreter.run(plan)
        self.record("contract.final", _result_payload(proposal.result))
        return proposal

    # ---- 3. attacks are checked by the same contracts -----------------------------------

    def check_hijack(
        self, vm: VerifiedMandate, proposal: Proposal, hijacked: ProposedCheckout, attack: str
    ) -> ContractResult:
        result = check_checkout(
            hijacked,
            mandate=vm,
            manifest=proposal.candidate.manifest,
            vault=self.vault,
            now=self.now(),
        )
        payload = {"attack": attack, **_result_payload(result)}
        try:
            build_order_request(
                result,
                manifest=proposal.candidate.manifest,
                custom_id="pv:probe",
                invoice_id="probe",
            )
            payload["order_builder"] = "BUILT"  # must never happen for a blocked checkout
        except CheckoutError as e:
            payload["order_builder"] = f"refused: {e}"
        self.record("contract.blocked" if not result.allowed else "contract.allowed", payload)
        return result

    # ---- 4. PayPal ----------------------------------------------------------------------

    def create_order(self, proposal: Proposal) -> OrderResult:
        if not proposal.result.allowed:
            raise PurchaseError("the final contract check did not allow this checkout")
        head = self.recorder.head(self.session_id)
        if head is None:  # pragma: no cover  (a session always has events by now)
            raise PurchaseError("empty session")
        custom_id = self.custom_id_format.render(head.event_hash)
        invoice_id = f"pv-{self.session_id}"
        body = build_order_request(
            proposal.result,
            manifest=proposal.candidate.manifest,
            custom_id=custom_id,
            invoice_id=invoice_id,
            experience_context=self.experience_context,
        )
        merchant = proposal.candidate.manifest.merchant_id
        self.record(
            "checkout.order_request",
            {"merchant_id": merchant, "body": body, "bound_event_seq": head.seq},
        )
        with self.paypal(merchant) as client:
            resp = client.post(
                "/v2/checkout/orders", json=body, operation_key=f"order:{self.session_id}"
            )
            order = client.get(f"/v2/checkout/orders/{resp.body['id']}").body
        self.record("paypal.order.created", {"merchant_id": merchant, "order": redact(order)})
        if order["purchase_units"][0].get("custom_id") != custom_id:
            raise PurchaseError("PayPal did not store the custom_id we sent")
        link = next(
            (
                x["href"]
                for x in order.get("links", [])
                if x.get("rel") in ("payer-action", "approve")
            ),
            None,
        )
        return OrderResult(
            merchant, order["id"], order["status"], link, custom_id, invoice_id, head.seq
        )

    def authorize_when_approved(self, order: OrderResult) -> dict[str, Any]:
        poll = float(self.settings["approval_poll_seconds"])
        deadline = time.monotonic() + float(self.settings["approval_timeout_seconds"])
        with self.paypal(order.merchant_id) as client:
            while True:
                status = client.get(f"/v2/checkout/orders/{order.order_id}").body["status"]
                if status == "APPROVED":
                    break
                if time.monotonic() > deadline:
                    raise PurchaseError(f"order {order.order_id} not approved in time ({status})")
                time.sleep(poll)
            resp = client.post(
                f"/v2/checkout/orders/{order.order_id}/authorize",
                operation_key=f"authorize:{self.session_id}",
            )
            auth_id = resp.body["purchase_units"][0]["payments"]["authorizations"][0]["id"]
            auth = client.get(f"/v2/payments/authorizations/{auth_id}").body
        result = {
            "order_id": order.order_id,
            "authorization_id": auth_id,
            "status": auth["status"],
            "custom_id": auth.get("custom_id"),
            "expiration_time": auth.get("expiration_time"),
        }
        self.record("paypal.authorization", result)
        if auth.get("custom_id") != order.custom_id:
            raise PurchaseError("authorization lost the custom_id binding")
        return result


def _result_payload(result: ContractResult) -> dict[str, Any]:
    return {
        "allowed": result.allowed,
        "violations": [
            {
                "field": v.field.value,
                "rule": v.rule.value,
                "detail": v.detail,
                "label": v.label.name if v.label is not None else None,
                "provenance": list(v.provenance),
            }
            for v in result.violations
        ],
    }
