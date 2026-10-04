"""S5: webhooks on a public URL (cloudflared quick tunnel), delivery, signature verification,
simulate-event, replay dedupe and forgery rejection.

Real events come from real actions:
  * voiding --void-authorization-id, if given (uncaptured auth) -> PAYMENT.AUTHORIZATION.VOIDED
  * a small operator payout to a sandbox buyer                      -> PAYMENT.PAYOUTSBATCH.SUCCESS

Webhook registrations created here are deleted at the end, along with any stale ones an earlier
run left behind (identified by the quick-tunnel host and our path prefix).

Run: python spikes/s5_webhooks.py [--void-authorization-id <id>]
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx
import uvicorn
from _common import (
    REPO_ROOT,
    ConfigError,
    PayPalClient,
    PayPalError,
    Spike,
    load_yaml,
    make_parser,
    merchant_client,
    operator_client,
    repo_sqlite_url,
    require_env,
    spike_config,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine

from paypal.webhooks import Outcome, WebhookStore, handle_delivery

TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
QUICK_TUNNEL_HOST = "trycloudflare.com"


class Receiver:
    """Local webhook endpoint. Records every delivery (accepted or not) for the spike."""

    def __init__(self, store: WebhookStore, prefix: str) -> None:
        self.store = store
        self.webhook_ids: dict[str, str] = {}
        self.verifiers: dict[str, PayPalClient] = {}
        self.deliveries: list[dict[str, Any]] = []
        self.app = FastAPI()
        self.app.get("/healthz")(lambda: {"ok": True})
        self.app.post(prefix + "/{app_label}")(self.receive)

    async def receive(self, app_label: str, request: Request) -> JSONResponse:
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if app_label not in self.webhook_ids:
            return JSONResponse({"outcome": "unknown app"}, status_code=404)
        result = handle_delivery(
            app_label=app_label,
            webhook_id=self.webhook_ids[app_label],
            headers=headers,
            raw_body=raw,
            verifier=self.verifiers[app_label],
            store=self.store,
        )
        self.deliveries.append(
            {
                "app": app_label,
                "headers": {k: v for k, v in headers.items() if k.startswith("paypal-")},
                "raw": raw,
                "outcome": result.outcome.value,
                "event_type": result.event_type,
                "event_id": result.event_id,
                "reason": result.reason,
            }
        )
        print(
            f"  <- {app_label} {result.event_type} {result.event_id}: {result.outcome.value}"
            + (f" ({result.reason})" if result.reason else "")
        )
        return JSONResponse({"outcome": result.outcome.value}, status_code=result.http_status)


def start_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    return server


def start_tunnel(binary: str, local_url: str, timeout: float) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(  # noqa: S603  (fixed, verified binary from bin/)
        [binary, "tunnel", "--no-autoupdate", "--url", local_url],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    for line in proc.stdout:
        m = TUNNEL_RE.search(line)
        if m:
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
            return proc, m.group(0)
        if time.monotonic() > deadline:
            break
    proc.terminate()
    raise RuntimeError("cloudflared did not report a tunnel URL")


def wait_public(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/healthz", timeout=5).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def delete_stale(client: PayPalClient, spike: Spike, prefix: str) -> None:
    hooks = client.get("/v1/notifications/webhooks").body.get("webhooks", [])
    for hook in hooks:
        if QUICK_TUNNEL_HOST in hook["url"] and prefix in hook["url"]:
            client.request("DELETE", f"/v1/notifications/webhooks/{hook['id']}")
            spike.log.append({"step": "deleted stale webhook", "id": hook["id"]})
            print(f"  deleted stale webhook {hook['id']}")


def register(
    client: PayPalClient, spike: Spike, url: str, event_types: list[str], op_key: str
) -> str:
    resp = client.post(
        "/v1/notifications/webhooks",
        json={"url": url, "event_types": [{"name": t} for t in event_types]},
        operation_key=op_key,
    )
    spike.step(f"register webhook {url}", resp)
    return str(resp.body["id"])


def generated_events(
    client: PayPalClient, since: str, event_type: str, resource_id: str
) -> list[str]:
    """Event ids PayPal itself generated for this app, type and resource since `since`."""
    resp = client.get(
        "/v1/notifications/webhooks-events",
        params={"page_size": 50, "start_time": since},
    )
    out = []
    # Filter here: the event_type query parameter returned nothing for an event that was in fact
    # generated and delivered (observed in S5), so it is not relied on.
    for e in resp.body.get("events", []):
        if e.get("event_type") != event_type:
            continue
        res = e.get("resource") or {}
        rid = res.get("id") or (res.get("batch_header") or {}).get("payout_batch_id")
        if rid == resource_id:
            out.append(str(e["id"]))
    return out


def wait_event(
    store: WebhookStore, event_type: str, resource_id: str | None, timeout: float, poll: float
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in store.events(event_type):
            if resource_id is None or row["resource_id"] == resource_id:
                return row
        time.sleep(poll)
    return None


def main() -> int:
    parser = make_parser(__doc__ or "")
    parser.add_argument("--void-authorization-id", help="uncaptured auth to void")
    args = parser.parse_args()
    cfg = spike_config()
    s5 = cfg["s5"]
    hooks_cfg = load_yaml("app.yaml")["webhooks"]
    prefix = hooks_cfg["path_prefix"]
    spike = Spike("S5")
    run_id = uuid.uuid4().hex[:12]
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))

    try:
        merchant_key = args.merchant or cfg["merchant"]
        merchant = merchant_client(merchant_key)
        operator = operator_client()
        receiver_email = require_env(s5["payout_receiver_env"])
        store = WebhookStore(create_engine(repo_sqlite_url(s5["store_url"])))
    except ConfigError as e:
        print(f"Config error: {e}")
        return 2

    apps = {
        "merchant": (merchant, hooks_cfg["event_types"]["merchant"]),
        "operator": (operator, hooks_cfg["event_types"]["operator"]),
    }
    receiver = Receiver(store, prefix)
    receiver.verifiers = {
        "merchant": merchant_client(merchant_key, ledger=False),
        "operator": operator_client(ledger=False),
    }
    server = start_server(receiver.app, s5["host"], int(s5["port"]))
    local = f"http://{s5['host']}:{s5['port']}"
    tunnel: subprocess.Popen[str] | None = None
    registered: dict[str, str] = {}

    try:
        tunnel, public = start_tunnel(
            str(REPO_ROOT / s5["cloudflared"]), local, float(s5["tunnel_timeout_seconds"])
        )
        print(f"Tunnel: {public}")
        spike.check("cloudflared quick tunnel up", True, public)
        spike.check(
            "public URL reaches local receiver",
            wait_public(public, float(s5["public_reachable_timeout_seconds"])),
        )

        for label, (client, types) in apps.items():
            delete_stale(client, spike, prefix)
            hook_id = register(
                client, spike, f"{public}{prefix}/{label}", types, f"s5:{run_id}:register:{label}"
            )
            registered[label] = hook_id
            receiver.webhook_ids[label] = hook_id
        spike.check("webhooks registered on merchant and operator apps", len(registered) == 2)

        # 1. simulate-event: does it deliver, and does its signature verify?
        sim = merchant.post(
            "/v1/notifications/simulate-event",
            json={
                "webhook_id": registered["merchant"],
                "event_type": s5["simulate_event_type"],
                "resource_version": "2.0",
            },
            operation_key=f"s5:{run_id}:simulate",
        )
        spike.step("simulate-event", sim)
        sim_id = sim.body.get("id")

        # 2. Real void -> PAYMENT.AUTHORIZATION.VOIDED (optional: needs an uncaptured auth)
        auth_id = args.void_authorization_id
        if auth_id:
            void = merchant.post(
                f"/v2/payments/authorizations/{auth_id}/void", operation_key=f"s5:void:{auth_id}"
            )
            spike.step("void authorization", void)
            got = merchant.get(f"/v2/payments/authorizations/{auth_id}")
            spike.check("authorization VOIDED", got.body["status"] == "VOIDED", got.body["status"])

        # 3. Real payout -> PAYMENT.PAYOUTSBATCH.SUCCESS
        batch_id = f"pv-spike-{uuid.uuid4().hex}"
        amount = f"{Decimal(str(s5['payout_amount'])).quantize(Decimal('0.01'))}"
        payout = operator.post(
            "/v1/payments/payouts",
            json={
                "sender_batch_header": {"sender_batch_id": batch_id, "recipient_type": "EMAIL"},
                "items": [
                    {
                        "recipient_type": "EMAIL",
                        "amount": {"value": amount, "currency": cfg["currency"]},
                        "receiver": receiver_email,
                        "sender_item_id": f"{batch_id}-0",
                    }
                ],
            },
            operation_key=f"s5:{run_id}:payout",
        )
        spike.step("create payout", payout)
        payout_batch_id = payout.body["batch_header"]["payout_batch_id"]

        timeout, poll = float(s5["delivery_timeout_seconds"]), float(s5["poll_interval_seconds"])
        print(f"\nWaiting up to {timeout:.0f}s for deliveries...")
        expected = [("operator", "PAYMENT.PAYOUTSBATCH.SUCCESS", payout_batch_id)]
        if auth_id:
            expected.append(("merchant", "PAYMENT.AUTHORIZATION.VOIDED", auth_id))
        received: dict[str, dict[str, Any] | None] = {}
        for _, event_type, resource_id in expected:
            received[event_type] = wait_event(store, event_type, resource_id, timeout, poll)

        # Did PayPal generate each expected event at all? Separates "not generated" from
        # "generated but not delivered" when something is missing.
        for label, event_type, resource_id in expected:
            generated = generated_events(apps[label][0], started_at, event_type, resource_id)
            row = received[event_type]
            spike.log.append({"step": f"{event_type} generated by PayPal", "event_ids": generated})
            spike.check(
                f"real {event_type} delivered and signature verified",
                row is not None,
                f"stored={row and row['event_id']} generated_by_paypal={generated or 'none'}",
            )

        # simulate-event result is an observation, not a requirement.
        sim_deliveries = [d for d in receiver.deliveries if d["event_id"] == sim_id]
        spike.log.append(
            {
                "step": "simulate-event observation",
                "event_id": sim_id,
                "deliveries": [
                    {k: d[k] for k in ("outcome", "reason", "event_type")} for d in sim_deliveries
                ],
            }
        )
        print(
            f"simulate-event {sim_id}: {[d['outcome'] for d in sim_deliveries] or 'not delivered'}"
        )

        # 4. Replay a genuine, real (not simulated) delivery: duplicate, stored once.
        genuine = next(
            (
                d
                for d in receiver.deliveries
                if d["outcome"] == "accepted" and d["event_id"] != sim_id
            ),
            None,
        )
        spike.check("a real delivery is available for replay tests", genuine is not None)
        if genuine:
            target = f"{local}{prefix}/{genuine['app']}"
            r = httpx.post(target, content=genuine["raw"], headers=genuine["headers"])
            spike.check(
                "replayed delivery acknowledged as duplicate",
                r.status_code == 200 and r.json()["outcome"] == Outcome.DUPLICATE.value,
                r.text,
            )
            count = sum(1 for e in store.events() if e["event_id"] == genuine["event_id"])
            spike.check("replayed event stored once", count == 1, str(count))

            # 5. Forgery: genuine headers, tampered body. PayPal must refuse the signature.
            event = json.loads(genuine["raw"])
            event["summary"] = "tampered by spike S5"
            r = httpx.post(target, content=json.dumps(event).encode(), headers=genuine["headers"])
            spike.check(
                "tampered body with genuine headers rejected",
                r.status_code == 400 and r.json()["outcome"] == Outcome.REJECTED.value,
                r.text,
            )
            # 6. Forgery: genuine body, made-up signature.
            fake_headers = {**genuine["headers"], "paypal-transmission-sig": "Zm9yZ2Vk"}
            r = httpx.post(target, content=genuine["raw"], headers=fake_headers)
            spike.check(
                "forged signature rejected",
                r.status_code == 400 and r.json()["outcome"] == Outcome.REJECTED.value,
                r.text,
            )

        spike.log.append(
            {
                "step": "deliveries",
                "items": [
                    {k: v for k, v in d.items() if k not in ("raw", "headers")}
                    for d in receiver.deliveries
                ],
            }
        )
    except PayPalError as e:
        spike.error("paypal call", e)
        spike.check("no PayPal error", False, e.summary())
    except Exception as e:
        spike.crashed(e)
    finally:
        for label, hook_id in registered.items():
            try:
                apps[label][0].request("DELETE", f"/v1/notifications/webhooks/{hook_id}")
                print(f"  deleted webhook {hook_id} ({label})")
            except PayPalError as e:
                spike.check(f"cleanup webhook {hook_id}", False, e.summary())
        if tunnel:
            tunnel.terminate()
        server.should_exit = True
    return spike.finish()


if __name__ == "__main__":
    raise SystemExit(main())
