"""Long-running webhook listener for spikes that need events over minutes (S6, refunds).

Opens a cloudflared quick tunnel, registers webhooks on the merchant and operator apps, and
verifies and stores every delivery (paypal/webhooks.py) until it receives SIGINT or SIGTERM or
`--duration` elapses. Registrations are deleted on exit. Each delivery is printed as one line.

Run: python spikes/webhook_listener.py [--merchant northwind] [--duration 3600]
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
import uuid

from _common import (
    REPO_ROOT,
    load_yaml,
    make_parser,
    merchant_client,
    operator_client,
    repo_sqlite_url,
    spike_config,
)
from s5_webhooks import Receiver, delete_stale, register, start_server, start_tunnel, wait_public
from sqlalchemy import create_engine

from paypal.client import PayPalError
from paypal.webhooks import WebhookStore


def main() -> int:
    parser = make_parser(__doc__ or "")
    parser.add_argument("--duration", type=float, default=3600, help="seconds before exiting")
    args = parser.parse_args()
    cfg = spike_config()
    s5 = cfg["s5"]
    hooks_cfg = load_yaml("app.yaml")["webhooks"]
    prefix = hooks_cfg["path_prefix"]
    merchant_key = args.merchant or cfg["merchant"]
    run_id = uuid.uuid4().hex[:12]

    apps = {
        "merchant": (merchant_client(merchant_key), hooks_cfg["event_types"]["merchant"]),
        "operator": (operator_client(), hooks_cfg["event_types"]["operator"]),
    }
    store = WebhookStore(create_engine(repo_sqlite_url(s5["store_url"])))
    receiver = Receiver(store, prefix)
    receiver.verifiers = {
        "merchant": merchant_client(merchant_key, ledger=False),
        "operator": operator_client(ledger=False),
    }
    server = start_server(receiver.app, s5["host"], int(s5["port"]))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    tunnel: subprocess.Popen[str] | None = None
    registered: dict[str, str] = {}
    try:
        tunnel, public = start_tunnel(
            str(REPO_ROOT / s5["cloudflared"]),
            f"http://{s5['host']}:{s5['port']}",
            float(s5["tunnel_timeout_seconds"]),
        )
        reachable = wait_public(public, float(s5["public_reachable_timeout_seconds"]))
        print(f"LISTENER tunnel={public} reachable={reachable}", flush=True)
        for label, (client, types) in apps.items():
            delete_stale(client, _NullSpike(), prefix)
            hook_id = register(
                client,
                _NullSpike(),
                f"{public}{prefix}/{label}",
                types,
                f"listener:{run_id}:register:{label}",
            )
            registered[label] = hook_id
            receiver.webhook_ids[label] = hook_id
            print(f"LISTENER registered {label} webhook {hook_id}", flush=True)
        print("LISTENER ready", flush=True)
        deadline = time.monotonic() + args.duration
        while not stop.is_set() and time.monotonic() < deadline:
            stop.wait(1)
    finally:
        for label, hook_id in registered.items():
            try:
                apps[label][0].request("DELETE", f"/v1/notifications/webhooks/{hook_id}")
                print(f"LISTENER deleted {label} webhook {hook_id}", flush=True)
            except PayPalError as e:
                print(f"LISTENER cleanup failed for {hook_id}: {e.summary()}", flush=True)
        if tunnel:
            tunnel.terminate()
        server.should_exit = True
    return 0


class _NullSpike:
    """register/delete_stale log into a Spike; the listener only prints."""

    def step(self, *args: object, **kwargs: object) -> None:
        pass

    log: list[dict[str, object]] = []


if __name__ == "__main__":
    raise SystemExit(main())
