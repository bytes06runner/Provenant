"""S8: does a JS SDK v6 PayPal button approve a server-created AUTHORIZE order?

A tiny local server: hands the page a browser-safe client token, creates the AUTHORIZE order
server-side (with custom_id binding), and authorizes it after the SDK's onApprove fires.
Checks are written to spikes/out/S8-*.json once the authorization is verified.

Run: python spikes/s8_jssdk/server.py   then open the printed URL and pay as a sandbox buyer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import uvicorn  # noqa: E402
from _common import (  # noqa: E402
    PayPalError,
    Spike,
    merchant_client,
    order_body,
    session_binding,
    spike_config,
)
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

cfg = spike_config()
s8 = cfg["s8"]
client = merchant_client(cfg["merchant"])
spike = Spike("S8")
created: dict[str, dict[str, str]] = {}  # order id -> {custom_id, invoice_id}
app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    html = (HERE / "index.html").read_text()
    return html.replace("{{SDK_SCRIPT_URL}}", s8["sdk_script_url"]).replace(
        "{{PRESENTATION_MODE}}", s8["presentation_mode"]
    )


@app.get("/api/client-token")
def client_token() -> dict[str, str]:
    try:
        token = client.browser_client_token(list(s8["client_token_domains"]))
    except PayPalError as e:
        spike.error("client token", e)
        spike.check("browser client token issued", False, e.summary())
        raise HTTPException(502, e.summary()) from e
    spike.check("browser client token issued", True)
    return {"clientToken": token}


@app.post("/api/orders")
def create_order() -> dict[str, str]:
    custom_id, invoice_id = session_binding()
    try:
        resp = client.post(
            "/v2/checkout/orders",
            json=order_body(cfg, custom_id, invoice_id, redirect=False),
            operation_key=f"s8:create:{invoice_id}",
        )
    except PayPalError as e:
        spike.error("create order", e)
        raise HTTPException(502, e.summary()) from e
    spike.step("create order (server, for JS SDK)", resp)
    order_id = resp.body["id"]
    created[order_id] = {"custom_id": custom_id, "invoice_id": invoice_id}
    spike.check("server created AUTHORIZE order", True, f"{order_id} {resp.body['status']}")
    return {"orderId": order_id}


@app.post("/api/orders/{order_id}/authorize")
def authorize(order_id: str) -> dict[str, Any]:
    spike.check(
        "onApprove orderId matches a server-created order",
        order_id in created,
        order_id,
    )
    if order_id not in created:
        spike.finish()
        raise HTTPException(404, "unknown order")
    binding = created[order_id]
    try:
        before = client.get(f"/v2/checkout/orders/{order_id}")
        spike.step("get order after onApprove", before)
        spike.check(
            "order APPROVED via JS SDK", before.body["status"] == "APPROVED", before.body["status"]
        )
        resp = client.post(
            f"/v2/checkout/orders/{order_id}/authorize", operation_key=f"s8:authorize:{order_id}"
        )
        spike.step("authorize order", resp)
        auth = resp.body["purchase_units"][0]["payments"]["authorizations"][0]
        got = client.get(f"/v2/payments/authorizations/{auth['id']}")
        spike.step("get authorization", got)
        spike.check("authorization CREATED", got.body["status"] == "CREATED", got.body["status"])
        spike.check(
            "custom_id and invoice_id on authorization",
            got.body.get("custom_id") == binding["custom_id"]
            and got.body.get("invoice_id") == binding["invoice_id"],
        )
        entry = resp.ledger_entry
        spike.check(
            "ledger recorded authorize",
            entry is not None and entry.resource_id == order_id,
            f"request_id={resp.request_id}",
        )
    except PayPalError as e:
        spike.error("authorize", e)
        spike.check("no PayPal error", False, e.summary())
        code = spike.finish()
        raise HTTPException(502, e.summary()) from e
    code = spike.finish()
    return {
        "result": "PASS" if code == 0 else "FAIL",
        "orderId": order_id,
        "authorizationId": auth["id"],
        "authorizationStatus": got.body["status"],
        "expirationTime": got.body.get("expiration_time"),
    }


@app.post("/api/events/{kind}")
def page_event(kind: str, payload: dict[str, Any]) -> dict[str, str]:
    """onCancel / onError from the page, recorded for the spike log."""
    spike.log.append({"step": f"page event: {kind}", "payload": payload})
    print(f"page event {kind}: {payload}")
    return {"ok": "recorded"}


if __name__ == "__main__":
    print(f"S8 test page: http://{s8['host']}:{s8['port']}/")
    uvicorn.run(app, host=s8["host"], port=int(s8["port"]), log_level="warning")
