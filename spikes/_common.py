"""Shared helpers for Phase 0 spikes. Spikes talk to the real PayPal sandbox only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paypal.client import PayPalClient, PayPalError, PayPalResponse  # noqa: E402
from paypal.config import (  # noqa: E402
    REPO_ROOT,
    ConfigError,
    http_settings,
    load_yaml,
    merchant_credentials,
    operator_credentials,
    require_env,
)
from paypal.ledger import RequestLedger  # noqa: E402
from paypal.redact import redact  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"


def money(value: Decimal, currency: str) -> dict[str, str]:
    return {"currency_code": currency, "value": f"{value.quantize(Decimal('0.01'))}"}


@dataclass
class Spike:
    name: str
    log: list[dict[str, Any]] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def step(self, label: str, resp: PayPalResponse | None = None, note: str = "") -> None:
        entry: dict[str, Any] = {"step": label, "at": datetime.now(UTC).isoformat()}
        if resp is not None:
            entry.update(
                status_code=resp.status_code,
                paypal_request_id=resp.request_id,
                debug_id=resp.debug_id,
                body=redact(resp.body),
            )
        if note:
            entry["note"] = note
        self.log.append(entry)
        code = f" [{resp.status_code}]" if resp is not None else ""
        print(f"\n--- {label}{code} {note}".rstrip())
        if resp is not None and resp.body is not None:
            print(json.dumps(redact(resp.body), indent=2)[:4000])

    def error(self, label: str, err: PayPalError) -> None:
        self.log.append(
            {"step": label, "error": str(err), "debug_id": err.debug_id, "body": redact(err.body)}
        )
        print(f"\n!!! {label} FAILED: {err}")
        print(json.dumps(redact(err.body), indent=2)[:4000])

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((label, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}".rstrip())
        return ok

    def crashed(self, err: Exception) -> None:
        """Record a non-PayPal failure (usually a wrong assumption about a response shape)."""
        import traceback

        tb = traceback.format_exc()
        self.log.append({"step": "unexpected error", "error": repr(err), "traceback": tb})
        print(tb)
        self.check("no unexpected error", False, repr(err))

    def finish(self) -> int:
        OUT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        passed = bool(self.checks) and all(ok for _, ok, _ in self.checks)
        path = OUT_DIR / f"{self.name}-{stamp}.json"
        path.write_text(
            json.dumps(
                {
                    "spike": self.name,
                    "result": "PASS" if passed else "FAIL",
                    "checks": [{"check": c, "ok": ok, "detail": d} for c, ok, d in self.checks],
                    "log": self.log,
                },
                indent=2,
            )
        )
        print(f"\n=== {self.name}: {'PASS' if passed else 'FAIL'}  (redacted log: {path})")
        return 0 if passed else 1


def make_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument(
        "--merchant", help="merchant key from config/merchants (default from spikes.yaml)"
    )
    p.add_argument("--open", action="store_true", help="open approval links in your browser")
    return p


def parse_args(description: str) -> argparse.Namespace:
    return make_parser(description).parse_args()


def spike_config() -> dict[str, Any]:
    return load_yaml("spikes.yaml")


def repo_sqlite_url(url: str) -> str:
    """Relative SQLite paths resolve from the repo root, not the caller's cwd."""
    OUT_DIR.mkdir(exist_ok=True)
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url[len(prefix) :].startswith("/"):
        url = prefix + str(REPO_ROOT / url[len(prefix) :])
    return url


def spike_ledger() -> RequestLedger:
    """Local SQLite request ledger for spikes (Postgres is used by the app from Phase 1)."""
    return RequestLedger.from_url(repo_sqlite_url(str(spike_config()["ledger_url"])))


def merchant_client(merchant_key: str, *, ledger: bool = True) -> PayPalClient:
    return PayPalClient(
        merchant_credentials(merchant_key),
        http_settings(),
        ledger=spike_ledger() if ledger else None,
    )


def operator_client(*, ledger: bool = True) -> PayPalClient:
    return PayPalClient(
        operator_credentials(), http_settings(), ledger=spike_ledger() if ledger else None
    )


def session_binding() -> tuple[str, str]:
    """Stand-in for a recorder session hash: custom_id and a unique invoice_id."""
    cfg = load_yaml("app.yaml")["paypal"]["custom_id"]
    session_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    custom_id = cfg["prefix"] + session_hash[: int(cfg["hash_hex_chars"])]
    if len(custom_id) > int(cfg["max_length"]):
        raise ConfigError("custom_id exceeds PayPal max length")
    return custom_id, f"pv-spike-{uuid.uuid4().hex}"


def order_body(
    cfg: dict[str, Any], custom_id: str, invoice_id: str, *, redirect: bool = True
) -> dict[str, Any]:
    """AUTHORIZE order from spike config. redirect=False omits return/cancel URLs, for the
    JS SDK flow where approval happens in a PayPal popup or overlay instead of a redirect."""
    experience: dict[str, Any] = {
        "brand_name": cfg["brand_name"],
        "shipping_preference": "NO_SHIPPING",
        "user_action": "CONTINUE",
    }
    if redirect:
        experience["return_url"] = require_env("PAYPAL_RETURN_URL")
        experience["cancel_url"] = require_env("PAYPAL_CANCEL_URL")
    currency = cfg["currency"]
    item = cfg["item"]
    unit = Decimal(str(item["unit_price"]))
    qty = int(item["quantity"])
    total = unit * qty
    return {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "reference_id": "pu-1",
                "custom_id": custom_id,
                "invoice_id": invoice_id,
                "description": item["name"],
                "amount": {
                    **money(total, currency),
                    "breakdown": {"item_total": money(total, currency)},
                },
                "items": [
                    {
                        "name": item["name"],
                        "sku": item["sku"],
                        "quantity": str(qty),
                        "unit_amount": money(unit, currency),
                        "category": "PHYSICAL_GOODS",
                    }
                ],
            }
        ],
        "payment_source": {"paypal": {"experience_context": experience}},
    }


def approval_link(order: dict[str, Any]) -> str | None:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return str(link["href"])
    return None


def wait_for_approval(
    client: PayPalClient, spike: Spike, order_ids: list[str], cfg: dict[str, Any]
) -> bool:
    """Poll until every order is APPROVED. A human approves each one in the browser."""
    interval = float(cfg["approval"]["poll_interval_seconds"])
    deadline = time.monotonic() + float(cfg["approval"]["timeout_seconds"])
    pending = set(order_ids)
    print(f"\nWaiting for buyer approval of {len(pending)} order(s). Log in as a sandbox buyer.")
    while pending and time.monotonic() < deadline:
        for oid in sorted(pending):
            status = client.get(f"/v2/checkout/orders/{oid}").body.get("status")
            if status == "APPROVED":
                pending.discard(oid)
                print(f"  {oid}: APPROVED")
        if pending:
            time.sleep(interval)
    for oid in order_ids:
        spike.check(f"order {oid} approved by buyer", oid not in pending)
    return not pending


def present_links(links: dict[str, str], open_browser: bool) -> None:
    print("\nApprove these in a browser while logged in as a sandbox Personal (buyer) account:")
    for oid, href in links.items():
        print(f"  {oid}: {href}")
        if open_browser:
            webbrowser.open(href)
