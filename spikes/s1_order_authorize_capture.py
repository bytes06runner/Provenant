"""S1: OAuth, then create, approve, authorize and capture an AUTHORIZE order with merchant
credentials. custom_id and invoice_id must be readable back from the order, the authorization
and the capture.

Run: python spikes/s1_order_authorize_capture.py [--merchant northwind] [--open]
"""

from __future__ import annotations

import sys

from _common import (
    ConfigError,
    PayPalError,
    Spike,
    approval_link,
    merchant_client,
    order_body,
    parse_args,
    present_links,
    session_binding,
    spike_config,
    wait_for_approval,
)


def main() -> int:
    args = parse_args(__doc__ or "")
    cfg = spike_config()
    merchant = args.merchant or cfg["merchant"]
    spike = Spike("S1")
    try:
        client = merchant_client(merchant)
    except ConfigError as e:
        print(f"Config error: {e}")
        return 2

    custom_id, invoice_id = session_binding()
    try:
        with client:
            # 1. Create
            resp = client.post("/v2/checkout/orders", json=order_body(cfg, custom_id, invoice_id))
            spike.step("create order", resp)
            order = resp.body
            oid = order["id"]
            spike.check("OAuth + order created", resp.status_code in (200, 201), order["status"])

            # 2. Read back binding fields before approval
            got = client.get(f"/v2/checkout/orders/{oid}")
            spike.step("get order (pre-approval)", got)
            pu = got.body["purchase_units"][0]
            spike.check("custom_id readable on order", pu.get("custom_id") == custom_id)
            spike.check("invoice_id readable on order", pu.get("invoice_id") == invoice_id)

            # 3. Human approval
            link = approval_link(order)
            if not spike.check("approval link present", link is not None):
                return spike.finish()
            present_links({oid: link}, args.open)  # type: ignore[dict-item]
            if not wait_for_approval(client, spike, [oid], cfg):
                return spike.finish()

            # 4. Authorize
            auth_resp = client.post(f"/v2/checkout/orders/{oid}/authorize")
            spike.step("authorize order", auth_resp)
            auth = auth_resp.body["purchase_units"][0]["payments"]["authorizations"][0]
            spike.check("order authorized", auth["status"] == "CREATED", auth["status"])

            auth_get = client.get(f"/v2/payments/authorizations/{auth['id']}")
            spike.step("get authorization", auth_get)
            spike.check("custom_id on authorization", auth_get.body.get("custom_id") == custom_id)
            spike.check(
                "invoice_id on authorization", auth_get.body.get("invoice_id") == invoice_id
            )
            spike.check(
                "authorization expiration_time reported",
                bool(auth_get.body.get("expiration_time")),
                str(auth_get.body.get("expiration_time")),
            )

            # 5. Capture
            cap_resp = client.post(
                f"/v2/payments/authorizations/{auth['id']}/capture",
                json={"final_capture": True},
            )
            spike.step("capture authorization", cap_resp)
            cap_id = cap_resp.body["id"]
            spike.check(
                "capture completed",
                cap_resp.body["status"] in ("COMPLETED", "PENDING"),
                cap_resp.body["status"],
            )

            cap_get = client.get(f"/v2/payments/captures/{cap_id}")
            spike.step("get capture", cap_get)
            spike.check("custom_id on capture", cap_get.body.get("custom_id") == custom_id)
            spike.check("invoice_id on capture", cap_get.body.get("invoice_id") == invoice_id)

            final = client.get(f"/v2/checkout/orders/{oid}")
            spike.step("get order (final)", final)
            spike.check(
                "custom_id still on order after capture",
                final.body["purchase_units"][0].get("custom_id") == custom_id,
            )
    except PayPalError as e:
        spike.error("paypal call", e)
        spike.check("no PayPal error", False, str(e))
    except Exception as e:
        spike.crashed(e)
    return spike.finish()


if __name__ == "__main__":
    sys.exit(main())
