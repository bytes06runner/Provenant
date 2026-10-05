"""Resume a recorded purchase session: wait for the buyer's approval, then authorize.

Everything needed comes from the session's Flight Recorder events (merchant, order id,
custom_id), and the authorization is checked to carry the same custom_id.

Run: python scripts/authorize_order.py --session s-xxxxxxxxxxxx
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine  # noqa: E402

from blackbox.recorder import FlightRecorder  # noqa: E402
from paypal.client import PayPalClient  # noqa: E402
from paypal.config import http_settings, load_env, load_yaml, merchant_credentials  # noqa: E402
from paypal.ledger import RequestLedger  # noqa: E402

VAR = REPO_ROOT / "var"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True)
    args = ap.parse_args()
    load_env()
    settings = load_yaml("app.yaml")["purchase"]
    recorder = FlightRecorder(create_engine(f"sqlite:///{VAR / 'provenant.db'}"))
    events = recorder.events(args.session)
    created = next(e for e in events if e.event_type == "paypal.order.created")
    order = created.payload["order"]
    merchant = created.payload["merchant_id"]
    order_id = order["id"]
    custom_id = order["purchase_units"][0]["custom_id"]
    ledger = RequestLedger.from_url(f"sqlite:///{VAR / 'ledger.db'}")
    with PayPalClient(merchant_credentials(merchant), http_settings(), ledger=ledger) as client:
        deadline = time.monotonic() + float(settings["approval_timeout_seconds"])
        while (
            status := client.get(f"/v2/checkout/orders/{order_id}").body["status"]
        ) != "APPROVED":
            if status == "COMPLETED":
                break
            if time.monotonic() > deadline:
                print(f"order {order_id} still {status}")
                return 2
            time.sleep(float(settings["approval_poll_seconds"]))
        if status != "COMPLETED":
            client.post(
                f"/v2/checkout/orders/{order_id}/authorize",
                operation_key=f"authorize:{args.session}",
            )
        o = client.get(f"/v2/checkout/orders/{order_id}").body
        auth_id = o["purchase_units"][0]["payments"]["authorizations"][0]["id"]
        auth = client.get(f"/v2/payments/authorizations/{auth_id}").body
    result = {
        "order_id": order_id,
        "authorization_id": auth_id,
        "status": auth["status"],
        "custom_id": auth.get("custom_id"),
        "expiration_time": auth.get("expiration_time"),
    }
    recorder.append(args.session, "paypal.authorization", result)
    print(json.dumps(result, indent=2))
    if auth.get("custom_id") != custom_id:
        print("ERROR: authorization lost the custom_id binding")
        return 1
    print(f"custom_id binding intact; chain head {recorder.verify(args.session)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
