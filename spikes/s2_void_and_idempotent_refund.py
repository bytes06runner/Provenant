"""S2: (a) void an authorization; (b) partially refund a capture, repeat the refund call with the
same PayPal-Request-Id, and confirm PayPal recorded exactly one refund.

Both orders are created up front so you approve them in one sitting.

Run:    python spikes/s2_void_and_idempotent_refund.py [--merchant northwind] [--open]
Resume: python spikes/s2_void_and_idempotent_refund.py --capture-id <id>
        (runs only part (b) against an existing, not yet refunded capture)
"""

from __future__ import annotations

import sys
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any

from _common import (
    ConfigError,
    PayPalClient,
    PayPalError,
    Spike,
    approval_link,
    make_parser,
    merchant_client,
    money,
    order_body,
    present_links,
    session_binding,
    spike_config,
    wait_for_approval,
)


def void_part(client: PayPalClient, spike: Spike, auth_id: str) -> None:
    void = client.post(f"/v2/payments/authorizations/{auth_id}/void")
    spike.step("void authorization", void)
    spike.check("void accepted", void.status_code in (200, 204), str(void.status_code))
    a = client.get(f"/v2/payments/authorizations/{auth_id}")
    spike.step("get voided authorization", a)
    spike.check("authorization status VOIDED", a.body["status"] == "VOIDED", a.body["status"])


def refund_part(client: PayPalClient, spike: Spike, cap_id: str, cfg: dict[str, Any]) -> None:
    # Capture POST returns a minimal body by default, so read the amount back with GET.
    before = client.get(f"/v2/payments/captures/{cap_id}")
    spike.step("get capture before refund", before)
    spike.check(
        "capture COMPLETED and not yet refunded",
        before.body["status"] == "COMPLETED",
        before.body["status"],
    )
    order_id = before.body["supplementary_data"]["related_ids"]["order_id"]
    captured = Decimal(before.body["amount"]["value"])
    currency = before.body["amount"]["currency_code"]
    refund_amt = (captured * Decimal(str(cfg["s2"]["refund_fraction"]))).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )

    request_id = str(uuid.uuid4())
    body = {"amount": money(refund_amt, currency), "note_to_payer": "Provenant spike S2"}
    refund_ids: list[str] = []
    for i in range(1 + int(cfg["s2"]["idempotent_repeats"])):
        r = client.post(f"/v2/payments/captures/{cap_id}/refund", json=body, request_id=request_id)
        spike.step(f"refund call #{i + 1} (same PayPal-Request-Id)", r)
        refund_ids.append(r.body["id"])

    spike.check(
        "every repeat returned the same refund id",
        len(set(refund_ids)) == 1,
        ",".join(refund_ids),
    )

    after = client.get(f"/v2/payments/captures/{cap_id}")
    spike.step("get capture after refunds", after)
    spike.check(
        "capture PARTIALLY_REFUNDED",
        after.body["status"] == "PARTIALLY_REFUNDED",
        after.body["status"],
    )

    o = client.get(f"/v2/checkout/orders/{order_id}")
    spike.step("get order after refunds", o)
    refunds = o.body["purchase_units"][0]["payments"].get("refunds", [])
    spike.check("order shows exactly one refund", len(refunds) == 1, str(len(refunds)))
    refunded_total = sum((Decimal(x["amount"]["value"]) for x in refunds), Decimal("0"))
    spike.check(
        "refunded total equals one partial refund",
        refunded_total == refund_amt,
        f"{refunded_total} vs {refund_amt} of {captured} {currency}",
    )


def full_run(client: PayPalClient, spike: Spike, cfg: dict[str, Any], open_links: bool) -> None:
    # Create two orders: one will be voided, the other captured then refunded.
    orders: dict[str, str] = {}
    links: dict[str, str] = {}
    for tag in ("void", "refund"):
        custom_id, invoice_id = session_binding()
        resp = client.post("/v2/checkout/orders", json=order_body(cfg, custom_id, invoice_id))
        spike.step(f"create order ({tag})", resp)
        orders[tag] = resp.body["id"]
        link = approval_link(resp.body)
        if not spike.check(f"approval link present ({tag})", link is not None):
            return
        links[resp.body["id"]] = link  # type: ignore[assignment]

    present_links(links, open_links)
    if not wait_for_approval(client, spike, list(orders.values()), cfg):
        return

    auth_ids: dict[str, str] = {}
    for tag, oid in orders.items():
        r = client.post(f"/v2/checkout/orders/{oid}/authorize")
        spike.step(f"authorize ({tag})", r)
        auth_ids[tag] = r.body["purchase_units"][0]["payments"]["authorizations"][0]["id"]

    void_part(client, spike, auth_ids["void"])

    cap = client.post(
        f"/v2/payments/authorizations/{auth_ids['refund']}/capture",
        json={"final_capture": True},
    )
    spike.step("capture", cap)
    refund_part(client, spike, cap.body["id"], cfg)


def main() -> int:
    parser = make_parser(__doc__ or "")
    parser.add_argument("--capture-id", help="resume: run only the refund part on this capture")
    args = parser.parse_args()
    cfg = spike_config()
    merchant = args.merchant or cfg["merchant"]
    spike = Spike("S2" if not args.capture_id else "S2b")
    try:
        # No ledger here on purpose: S2 tests PayPal's own PayPal-Request-Id handling, so it
        # must be able to resend a request our ledger would refuse.
        client = merchant_client(merchant, ledger=False)
    except ConfigError as e:
        print(f"Config error: {e}")
        return 2

    try:
        with client:
            if args.capture_id:
                refund_part(client, spike, args.capture_id, cfg)
            else:
                full_run(client, spike, cfg, args.open)
    except PayPalError as e:
        spike.error("paypal call", e)
        spike.check("no PayPal error", False, str(e))
    except Exception as e:
        spike.crashed(e)
    return spike.finish()


if __name__ == "__main__":
    sys.exit(main())
