"""Onboard each simulated merchant with Provenant (CLAUDE.md section 2).

For every merchant in config/merchants/ that has REST credentials in .env:
  1. create (or load) its Ed25519 signing key in var/keys/ (the merchant side keeps it)
  2. learn its PayPal merchant id, the payee its orders pay. PayPal does not return it for an
     app's own token, so one unapproved AUTHORIZE order is created and the payee read back.
     The order is never approved and expires on its own. It goes through the request ledger, so
     re-running registration never creates a second probe.
  3. record public key, key id, payee and storefront URL in var/registry.json

Run: python scripts/register_merchants.py
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


from lineage.signing import key_id, public_key_b64  # noqa: E402
from merchants.keystore import load_or_create  # noqa: E402
from merchants.registry import MerchantRecord, save_records  # noqa: E402
from paypal.client import AlreadyCompleted, PayPalClient  # noqa: E402
from paypal.config import (  # noqa: E402
    CONFIG_DIR,
    ConfigError,
    http_settings,
    load_env,
    load_yaml,
    merchant_credentials,
    require_env,
)
from paypal.ledger import RequestLedger  # noqa: E402

KEYS_DIR = REPO_ROOT / "var" / "keys"
REGISTRY = REPO_ROOT / "var" / "registry.json"


def payee_id(client: PayPalClient, merchant: str, ledger: RequestLedger) -> str:
    op = f"register:{merchant}:payee-probe"
    try:
        resp = client.post(
            "/v2/checkout/orders",
            json={
                "intent": "AUTHORIZE",
                "purchase_units": [
                    {
                        "reference_id": "registration-probe",
                        "invoice_id": f"pv-register-{merchant}-{uuid.uuid4().hex[:12]}",
                        "amount": {"currency_code": "USD", "value": "1.00"},
                    }
                ],
            },
            operation_key=op,
        )
        order_id = resp.body["id"]
    except AlreadyCompleted as done:
        order_id = done.entry.resource_id
    order = client.get(f"/v2/checkout/orders/{order_id}").body
    return str(order["purchase_units"][0]["payee"]["merchant_id"])


def main() -> int:
    load_env()
    base = require_env("MERCHANTS_BASE_URL").rstrip("/")
    (REPO_ROOT / "var").mkdir(exist_ok=True)
    ledger = RequestLedger.from_url(f"sqlite:///{REPO_ROOT / 'var' / 'ledger.db'}")
    records: dict[str, MerchantRecord] = {}
    for path in sorted((CONFIG_DIR / "merchants").glob("*.yaml")):
        profile = load_yaml(f"merchants/{path.name}")
        key = profile["key"]
        try:
            creds = merchant_credentials(key)
        except ConfigError as e:
            print(f"{key}: skipped ({e})")
            continue
        signing = load_or_create(KEYS_DIR, key)
        pub = signing.public_key()
        with PayPalClient(creds, http_settings(), ledger=ledger) as client:
            merchant_id = payee_id(client, key, ledger)
        records[key] = MerchantRecord(
            merchant_id=key,
            display_name=profile["display_name"],
            paypal_merchant_id=merchant_id,
            key_id=key_id(pub),
            public_key=public_key_b64(pub),
            base_url=f"{base}/m/{key}",
        )
        print(f"{key}: payee {merchant_id}, key {key_id(pub)}")
    save_records(REGISTRY, records)
    print(f"wrote {REGISTRY.relative_to(REPO_ROOT)} ({len(records)} merchants)")
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
