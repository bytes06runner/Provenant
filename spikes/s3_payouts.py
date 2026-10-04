"""S3: liability-pool payout from the Provenant operator account to sandbox buyer emails.

Proves: batch creation, the request ledger refusing a second send of the same payout, batch
status polling to a terminal state, and per-item transaction ids. Payout webhooks are checked
in S5, which needs a public URL.

Run: python spikes/s3_payouts.py
"""

from __future__ import annotations

import sys
import time
import uuid
from decimal import Decimal
from typing import Any

from _common import (
    ConfigError,
    PayPalClient,
    PayPalError,
    Spike,
    operator_client,
    parse_args,
    require_env,
    spike_config,
)

from paypal.client import AlreadyCompleted
from paypal.redact import mask_email


def payout_body(cfg: dict[str, Any], batch_id: str, receivers: list[str]) -> dict[str, Any]:
    s3 = cfg["s3"]
    amount = f"{Decimal(str(s3['amount'])).quantize(Decimal('0.01'))}"
    return {
        "sender_batch_header": {
            "sender_batch_id": batch_id,
            "recipient_type": "EMAIL",
            "email_subject": s3["email_subject"],
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {"value": amount, "currency": cfg["currency"]},
                "receiver": email,
                "note": s3["note"],
                "sender_item_id": f"{batch_id}-{i}",
            }
            for i, email in enumerate(receivers)
        ],
    }


def poll_batch(client: PayPalClient, spike: Spike, batch_id: str, s3: dict[str, Any]) -> Any:
    terminal = set(s3["terminal_batch_statuses"])
    deadline = time.monotonic() + float(s3["timeout_seconds"])
    while True:
        resp = client.get(f"/v1/payments/payouts/{batch_id}")
        status = resp.body["batch_header"]["batch_status"]
        print(f"  batch {batch_id}: {status}")
        if status in terminal or time.monotonic() > deadline:
            spike.step("get payout batch (final)", resp)
            return resp.body
        time.sleep(float(s3["poll_interval_seconds"]))


def main() -> int:
    parse_args(__doc__ or "")
    cfg = spike_config()
    s3 = cfg["s3"]
    spike = Spike("S3")
    try:
        client = operator_client()  # loads .env
        receivers = [require_env(name) for name in s3["receiver_envs"]]
    except ConfigError as e:
        print(f"Config error: {e}")
        return 2
    print("Receivers:", ", ".join(mask_email(r) for r in receivers))

    batch_id = f"pv-spike-{uuid.uuid4().hex}"
    op_key = f"s3:payout:{batch_id}"
    try:
        with client:
            resp = client.post(
                "/v1/payments/payouts",
                json=payout_body(cfg, batch_id, receivers),
                operation_key=op_key,
            )
            spike.step("create payout batch", resp)
            header = resp.body["batch_header"]
            payout_batch_id = header["payout_batch_id"]
            spike.check("payout batch accepted (2xx)", 200 <= resp.status_code < 300)
            entry = resp.ledger_entry
            spike.check(
                "ledger stored request id and batch id",
                entry is not None
                and entry.resource_id == payout_batch_id
                and entry.request_id == resp.request_id,
                f"request_id={resp.request_id} batch={payout_batch_id}",
            )

            # Our ledger, not PayPal, must refuse to send this payout twice.
            try:
                client.post(
                    "/v1/payments/payouts",
                    json=payout_body(cfg, batch_id, receivers),
                    operation_key=op_key,
                )
                spike.check("ledger refused duplicate payout", False, "second POST was sent")
            except AlreadyCompleted as done:
                spike.check(
                    "ledger refused duplicate payout",
                    done.entry.resource_id == payout_batch_id,
                    f"AlreadyCompleted -> {done.entry.resource_id}",
                )

            final = poll_batch(client, spike, payout_batch_id, s3)
            status = final["batch_header"]["batch_status"]
            spike.check("batch reached SUCCESS", status == "SUCCESS", status)

            items = final.get("items", [])
            spike.check("one item per receiver", len(items) == len(receivers), str(len(items)))
            for item in items:
                item_id = item["payout_item_id"]
                got = client.get(f"/v1/payments/payouts-item/{item_id}")
                spike.step(f"get payout item {item_id}", got)
                tstatus = got.body.get("transaction_status")
                spike.check(
                    f"item {item_id} paid",
                    tstatus in ("SUCCESS", "UNCLAIMED"),
                    f"{tstatus} txn={got.body.get('transaction_id')}",
                )
    except PayPalError as e:
        spike.error("paypal call", e)
        spike.check("no PayPal error", False, str(e))
    except Exception as e:
        spike.crashed(e)
    return spike.finish()


if __name__ == "__main__":
    sys.exit(main())
