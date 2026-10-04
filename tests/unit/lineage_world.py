"""A small signed world for Lineage tests: one user, an honest merchant, an attacker merchant.

Test-only data. Application code never contains catalogs, keys or addresses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lineage.mandate import IntentMandate, VerifiedMandate, sign_mandate, verify_mandate
from lineage.manifest import (
    MerchantKeyRegistry,
    MerchantManifest,
    VerifiedManifest,
    sign_manifest,
    verify_manifest,
)
from lineage.signing import key_id
from lineage.vault import Address, AddressVault

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = "user-a"


def mandate_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mandate_id": "m-1",
        "user_id": USER,
        "nonce": "n-0123456789abcdef",
        "issued_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=1),
        "category": "trail-running-shoes",
        "required_attributes": {"color": "black", "size_us": "10"},
        "forbidden_attributes": {"material": ["leather"]},
        "max_unit_price": "120.00",
        "max_total": "130.00",
        "currency": "USD",
        "quantity": 1,
        "merchant_allowlist": None,
        "ship_to_ref": "home",
        "autonomy_mode": "human_present",
    }
    base.update(overrides)
    return base


def manifest_payload(merchant_id: str, payee: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "merchant_id": merchant_id,
        "display_name": merchant_id.title(),
        "paypal_merchant_id": payee,
        "version": 1,
        "issued_at": "2026-10-01T00:00:00Z",
        "currency": "USD",
        "shipping_flat": "5.00",
        "tax_rate": "0.00",
        "catalog": [
            {
                "sku": "TRAIL-BLK-10",
                "title": "Trail runner, black, US 10",
                "attributes": {"color": "black", "size_us": "10", "material": "mesh"},
                "price": "99.00",
                "currency": "USD",
                "stock": 5,
            },
            {
                "sku": "TRAIL-NVY-10",
                "title": "Trail runner, navy, US 10",
                "attributes": {"color": "navy", "size_us": "10", "material": "mesh"},
                "price": "89.00",
                "currency": "USD",
                "stock": 5,
            },
            {
                "sku": "TRAIL-BLK-10-LTH",
                "title": "Trail runner, black leather, US 10",
                "attributes": {"color": "black", "size_us": "10", "material": "leather"},
                "price": "110.00",
                "currency": "USD",
                "stock": 5,
            },
            {
                "sku": "TRAIL-BLK-10-PRO",
                "title": "Trail runner pro, black, US 10",
                "attributes": {"color": "black", "size_us": "10", "material": "mesh"},
                "price": "125.00",
                "currency": "USD",
                "stock": 5,
            },
        ],
        "policy": {
            "return_window_days": 30,
            "refund_caps": {
                "fulfillment_fault": "1.00",
                "misrepresentation": "1.00",
                "decision_fault": "0.50",
            },
            "restocking_fee": "0.00",
            "partial_refunds_preauthorized": True,
        },
    }
    base.update(overrides)
    return base


HOME = Address(
    full_name="Buyer A",
    address_line_1="1 Main St",
    admin_area_2="San Jose",
    admin_area_1="CA",
    postal_code="95131",
    country_code="US",
)
ELSEWHERE = Address(
    full_name="Drop Point",
    address_line_1="99 Exfil Rd",
    admin_area_2="Reno",
    admin_area_1="NV",
    postal_code="89501",
    country_code="US",
)


@dataclass
class World:
    user_key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    honest_key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    attacker_key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    registry: MerchantKeyRegistry = field(default_factory=MerchantKeyRegistry)
    vault: AddressVault = field(default_factory=AddressVault)

    def __post_init__(self) -> None:
        for mid, key in (("northwind", self.honest_key), ("attacker", self.attacker_key)):
            self.registry.register(mid, key_id(key.public_key()), key.public_key())
        self.vault.save_confirmed(USER, "home", HOME)
        self.vault.save_confirmed(USER, "office", ELSEWHERE)

    @property
    def user_keys(self) -> dict[str, Any]:
        pub = self.user_key.public_key()
        return {key_id(pub): pub}

    def mandate(self, **overrides: Any) -> VerifiedMandate:
        env = sign_mandate(IntentMandate(**mandate_payload(**overrides)), self.user_key)
        return verify_mandate(env, user_id=USER, user_keys=self.user_keys, now=NOW)

    def manifest(self, merchant_id: str = "northwind", **overrides: Any) -> VerifiedManifest:
        payee = {"northwind": "NWPAYEE123", "attacker": "ATKPAYEE666"}[merchant_id]
        key = self.honest_key if merchant_id == "northwind" else self.attacker_key
        m = MerchantManifest(**manifest_payload(merchant_id, payee, **overrides))
        return verify_manifest(
            sign_manifest(m, key), merchant_id=merchant_id, registry=self.registry
        )
