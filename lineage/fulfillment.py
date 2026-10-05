"""Merchant fulfillment for a recorded purchase: ship, capture, add tracking (CLAUDE.md 5.1).

The merchant's shipment record (what it says it shipped) is recorded verbatim; Blackbox later
weighs it against the user's evidence. Capture uses the merchant's credentials through the
request ledger and is verified with a GET. Tracking is added after capture (PayPal's tracking
call needs the capture id); a tracking failure is recorded but does not undo the capture.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from blackbox.recorder import FlightRecorder
from paypal.client import PayPalClient, PayPalError


class FulfillmentError(RuntimeError):
    pass


def fulfill_and_capture(
    *,
    recorder: FlightRecorder,
    session_id: str,
    storefront_url: str,
    http: httpx.Client,
    paypal: Callable[[str], PayPalClient],
    force_wrong_variant: bool = False,
) -> dict[str, Any]:
    events = recorder.events(session_id)
    created = next((e for e in events if e.event_type == "paypal.order.created"), None)
    auth = next((e for e in events if e.event_type == "paypal.authorization"), None)
    proposed = next((e for e in events if e.event_type == "plan.proposed"), None)
    if created is None or auth is None or proposed is None:
        raise FulfillmentError("the purchase must be authorized before it can be fulfilled")
    merchant = created.payload["merchant_id"]
    order_id = created.payload["order"]["id"]
    sku = proposed.payload["candidate"]["sku"]

    resp = http.post(
        f"{storefront_url}/fulfill",
        json={"order_ref": order_id, "sku": sku, "force_wrong_variant": force_wrong_variant},
    )
    resp.raise_for_status()
    shipment = resp.json()
    recorder.append(
        session_id,
        "fulfillment.shipped",
        {"merchant_id": merchant, **shipment, "planted": force_wrong_variant},
    )

    auth_id = auth.payload["authorization_id"]
    with paypal(merchant) as client:
        cap = client.post(
            f"/v2/payments/authorizations/{auth_id}/capture",
            json={"final_capture": True},
            operation_key=f"capture:{session_id}",
        )
        capture = client.get(f"/v2/payments/captures/{cap.body['id']}").body
        recorder.append(
            session_id,
            "paypal.capture",
            {
                "capture_id": capture["id"],
                "status": capture["status"],
                "amount": capture["amount"],
                "custom_id": capture.get("custom_id"),
            },
        )
        tracking: dict[str, Any]
        try:
            t = client.post(
                f"/v2/checkout/orders/{order_id}/track",
                json={
                    "capture_id": capture["id"],
                    "tracking_number": shipment["tracking_number"],
                    "carrier": "OTHER",
                    "carrier_name_other": "Provenant simulated carrier",
                    "notify_payer": False,
                },
                operation_key=f"track:{session_id}",
            )
            tracking = {"ok": True, "status_code": t.status_code}
        except PayPalError as e:
            tracking = {"ok": False, "error": e.summary()}
        recorder.append(session_id, "paypal.tracking", tracking)
    return {
        "shipment": shipment,
        "capture_id": capture["id"],
        "capture_status": capture["status"],
        "amount": capture["amount"],
        "tracking": tracking,
    }


def authorize_recorded(
    *,
    recorder: FlightRecorder,
    session_id: str,
    paypal: Callable[[str], PayPalClient],
    poll_seconds: float,
    timeout_seconds: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict[str, Any]:
    """Wait for the buyer's approval of a recorded order, then authorize it (idempotent)."""
    created = next(e for e in recorder.events(session_id) if e.event_type == "paypal.order.created")
    merchant = created.payload["merchant_id"]
    order_id = created.payload["order"]["id"]
    custom_id = created.payload["order"]["purchase_units"][0]["custom_id"]
    deadline = clock() + timeout_seconds
    with paypal(merchant) as client:
        while True:
            order = client.get(f"/v2/checkout/orders/{order_id}").body
            if order["status"] in ("APPROVED", "COMPLETED"):
                break
            if clock() > deadline:
                raise FulfillmentError(f"order {order_id} still {order['status']}")
            sleep(poll_seconds)
        if order["status"] == "APPROVED":
            client.post(
                f"/v2/checkout/orders/{order_id}/authorize", operation_key=f"authorize:{session_id}"
            )
            order = client.get(f"/v2/checkout/orders/{order_id}").body
        auth_id = order["purchase_units"][0]["payments"]["authorizations"][0]["id"]
        auth = client.get(f"/v2/payments/authorizations/{auth_id}").body
    result = {
        "order_id": order_id,
        "authorization_id": auth_id,
        "status": auth["status"],
        "custom_id": auth.get("custom_id"),
        "expiration_time": auth.get("expiration_time"),
    }
    if not any(e.event_type == "paypal.authorization" for e in recorder.events(session_id)):
        recorder.append(session_id, "paypal.authorization", result)
    if auth.get("custom_id") != custom_id:
        raise FulfillmentError("authorization lost the custom_id binding")
    return result
