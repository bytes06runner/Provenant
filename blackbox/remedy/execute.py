"""Execute an approved remedy plan on PayPal rails.

Each action goes through the request ledger with an operation key derived from the case, so an
approval clicked twice, or a crash and retry, can never move money twice. Every action is read
back with a GET and handed to the reconciliation poller until its status is final.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from blackbox.reconcile import ReconciliationPoller
from blackbox.remedy import RemedyPlan
from paypal.client import AlreadyCompleted, PayPalClient


class RemedyError(RuntimeError):
    pass


def execute(
    plan: RemedyPlan,
    *,
    case_id: str,
    merchant_id: str,
    authorization_id: str | None,
    capture_id: str | None,
    currency: str,
    buyer_email: str,
    paypal: Callable[[str], PayPalClient],  # "operator" or a merchant id -> client
    poller: ReconciliationPoller,
    record: Callable[[str, dict[str, Any]], None],
) -> list[dict[str, Any]]:
    if plan.status != "proposed":
        raise RemedyError(f"plan is {plan.status}; nothing to execute")
    results = []
    for action in plan.actions:
        key = f"remedy:{case_id}:{action.kind}"
        if action.kind == "void":
            if authorization_id is None:
                raise RemedyError("no authorization to void")
            with paypal(merchant_id) as c:
                try:
                    c.post(
                        f"/v2/payments/authorizations/{authorization_id}/void", operation_key=key
                    )
                except AlreadyCompleted:
                    pass
                status = c.get(f"/v2/payments/authorizations/{authorization_id}").body["status"]
            res = {"kind": "void", "resource_id": authorization_id, "status": status}
            poller.track("authorization", authorization_id, merchant_id, case_id)
        elif action.kind == "refund":
            if capture_id is None:
                raise RemedyError("no capture to refund")
            with paypal(merchant_id) as c:
                try:
                    r = c.post(
                        f"/v2/payments/captures/{capture_id}/refund",
                        json={
                            "amount": {"currency_code": currency, "value": action.amount},
                            "note_to_payer": f"Provenant ruling {case_id}: {action.reason}"[:255],
                        },
                        operation_key=key,
                    )
                    refund_id = r.body["id"]
                except AlreadyCompleted as done:
                    refund_id = str(done.entry.resource_id)
                refund = c.get(f"/v2/payments/refunds/{refund_id}").body
            res = {
                "kind": "refund",
                "resource_id": refund_id,
                "status": refund["status"],
                "amount": refund["amount"]["value"],
                "party": "merchant",
            }
            poller.track("refund", refund_id, merchant_id, case_id)
        elif action.kind == "payout":
            with paypal("operator") as c:
                try:
                    p = c.post(
                        "/v1/payments/payouts",
                        json={
                            "sender_batch_header": {
                                "sender_batch_id": f"pv-{case_id}",
                                "email_subject": "Provenant ruling payout",
                            },
                            "items": [
                                {
                                    "recipient_type": "EMAIL",
                                    "amount": {"value": action.amount, "currency": currency},
                                    "receiver": buyer_email,
                                    "note": f"Provenant ruling {case_id}: {action.reason}"[:4000],
                                    "sender_item_id": f"{case_id}-agent-share",
                                }
                            ],
                        },
                        operation_key=key,
                    )
                    batch_id = p.body["batch_header"]["payout_batch_id"]
                except AlreadyCompleted as done:
                    batch_id = str(done.entry.resource_id)
                batch = c.get(f"/v1/payments/payouts/{batch_id}").body
            res = {
                "kind": "payout",
                "resource_id": batch_id,
                "status": batch["batch_header"]["batch_status"],
                "amount": action.amount,
                "party": "operator",
            }
            poller.track("payout", batch_id, "operator", case_id)
        else:
            raise RemedyError(f"unknown action {action.kind!r}")
        record("remedy.executed", res)
        results.append(res)
    return results
