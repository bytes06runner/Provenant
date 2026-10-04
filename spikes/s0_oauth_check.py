"""S0: confirm every configured REST app can get an OAuth token, and list notable scopes.

Run: python spikes/s0_oauth_check.py
"""

from __future__ import annotations

import sys

import httpx
from _common import ConfigError, Spike, load_yaml

from paypal.config import (
    CONFIG_DIR,
    Credentials,
    http_settings,
    merchant_credentials,
    operator_credentials,
)

# Scopes later phases depend on; reported, not required, at this stage.
NOTABLE_SCOPES = {
    "payouts": "https://uri.paypal.com/payments/payouts",
    "refund": "https://uri.paypal.com/services/payments/refund",
    "disputes": "https://uri.paypal.com/services/disputes/read-seller",
    "dispute_create": "https://uri.paypal.com/services/disputes/create",
    "webhooks": "https://uri.paypal.com/services/applications/webhooks",
    "vault": "https://uri.paypal.com/services/vault/payment-tokens/readwrite",
}


def all_credentials() -> list[Credentials]:
    creds = [
        merchant_credentials(load_yaml(f"merchants/{p.name}")["key"])
        for p in sorted((CONFIG_DIR / "merchants").glob("*.yaml"))
    ]
    return [*creds, operator_credentials()]


def main() -> int:
    spike = Spike("S0")
    try:
        settings = http_settings()
        creds = all_credentials()
    except ConfigError as e:
        print(f"Config error: {e}")
        return 2
    for c in creds:
        resp = httpx.post(
            f"{settings.api_base}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(c.client_id, c.client_secret),
            timeout=settings.timeout_seconds,
        )
        if resp.status_code != 200:
            spike.check(f"{c.label} token", False, f"{resp.status_code} {resp.text[:200]}")
            continue
        body = resp.json()
        scopes = set(body.get("scope", "").split())
        have = [k for k, uri in NOTABLE_SCOPES.items() if uri in scopes]
        missing = [k for k in NOTABLE_SCOPES if k not in have]
        spike.log.append({"app": c.label, "app_id": body.get("app_id"), "scopes": sorted(scopes)})
        spike.check(
            f"{c.label} token",
            True,
            f"app_id={body.get('app_id')} has={have} missing={missing}",
        )
    return spike.finish()


if __name__ == "__main__":
    sys.exit(main())
