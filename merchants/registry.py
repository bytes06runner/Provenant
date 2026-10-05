"""Merchant registry: what Provenant learned about each merchant at onboarding.

Written by scripts/register_merchants.py to `var/registry.json` (per environment, gitignored):
the merchant's public signing key and its PayPal merchant id (the order payee). Lineage builds
its MerchantKeyRegistry from here, so manifest signatures verify only against keys registered
out of band, never against keys a storefront serves about itself.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from lineage.manifest import MerchantKeyRegistry
from lineage.signing import key_id, load_public_key


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MerchantRecord:
    merchant_id: str
    display_name: str
    paypal_merchant_id: str
    key_id: str
    public_key: str  # base64url raw Ed25519
    base_url: str  # where the storefront serves its manifest and pages


def load_records(path: Path) -> dict[str, MerchantRecord]:
    if not path.exists():
        raise RegistryError(f"{path} not found: run scripts/register_merchants.py")
    data = json.loads(path.read_text())
    return {k: MerchantRecord(**v) for k, v in data["merchants"].items()}


def save_records(path: Path, records: dict[str, MerchantRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"merchants": {k: asdict(v) for k, v in sorted(records.items())}}, indent=2)
    )


def key_registry(records: dict[str, MerchantRecord]) -> MerchantKeyRegistry:
    reg = MerchantKeyRegistry()
    for r in records.values():
        pub = load_public_key(r.public_key)
        if key_id(pub) != r.key_id:
            raise RegistryError(f"{r.merchant_id}: key id does not match the public key")
        reg.register(r.merchant_id, r.key_id, pub)
    return reg
