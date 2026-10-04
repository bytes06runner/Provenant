"""Signed Merchant Manifest: the merchant side of trust (CLAUDE.md section 4.2).

A manifest is the merchant's machine-readable identity, PayPal payee, catalog and policy,
JCS-canonicalized and Ed25519-signed with the key registered at onboarding. Values read from a
verified manifest are MERCHANT_SIGNED(merchant_id, manifest_hash): non-repudiable, so a false
signed attribute is provable later.

Product pages, reviews and banners are not part of the manifest and are always UNTRUSTED.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lineage.labels import MINT, Labeled, mint_merchant_signed
from lineage.money import parse_amount, parse_currency
from lineage.signing import SignatureError, SignedEnvelope, sign, verify

PURPOSE = "provenant/manifest/v1"


class ManifestError(Exception):
    pass


class Product(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sku: str = Field(min_length=1, max_length=127)
    title: str = Field(min_length=1, max_length=127)
    attributes: dict[str, str] = Field(default_factory=dict)
    price: str
    currency: str
    stock: int = Field(ge=0)

    @field_validator("price")
    @classmethod
    def _price(cls, v: str) -> str:
        parse_amount(v)
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        return parse_currency(v)


class RefundCaps(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fulfillment_fault: str  # max refund as a fraction of the item total, "1.00" = full
    misrepresentation: str
    decision_fault: str

    @field_validator("fulfillment_fault", "misrepresentation", "decision_fault")
    @classmethod
    def _fraction(cls, v: str) -> str:
        if not Decimal("0") <= parse_amount(v) <= Decimal("1"):
            raise ValueError("refund cap must be a fraction between 0 and 1")
        return v


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    return_window_days: int = Field(ge=0, le=365)
    refund_caps: RefundCaps
    restocking_fee: str = "0.00"  # fraction
    partial_refunds_preauthorized: bool = False


class MerchantManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    merchant_id: str = Field(min_length=1, max_length=64)  # Provenant's id for the merchant
    display_name: str = Field(min_length=1, max_length=127)
    paypal_merchant_id: str = Field(min_length=1, max_length=64)  # the order's payee
    version: int = Field(ge=1)
    issued_at: str
    currency: str
    shipping_flat: str = "0.00"
    tax_rate: str = "0.00"  # fraction applied to item total
    catalog: list[Product] = Field(min_length=1)
    policy: Policy

    @field_validator("shipping_flat", "tax_rate")
    @classmethod
    def _amounts(cls, v: str) -> str:
        parse_amount(v)
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        return parse_currency(v)

    @model_validator(mode="after")
    def _consistent(self) -> MerchantManifest:
        skus = [p.sku for p in self.catalog]
        if len(skus) != len(set(skus)):
            raise ValueError("duplicate sku in catalog")
        if any(p.currency != self.currency for p in self.catalog):
            raise ValueError("catalog currency must match manifest currency")
        return self


def sign_manifest(manifest: MerchantManifest, merchant_key: Ed25519PrivateKey) -> SignedEnvelope:
    return sign(PURPOSE, manifest.model_dump(mode="json"), merchant_key)


@dataclass(frozen=True)
class VerifiedManifest:
    manifest: MerchantManifest
    envelope: SignedEnvelope

    @property
    def merchant_id(self) -> str:
        return self.manifest.merchant_id

    @property
    def hash(self) -> str:
        return self.envelope.payload_hash

    @property
    def ref(self) -> str:
        return f"manifest:{self.merchant_id}:{self.hash}"

    def _field[T](self, value: T, path: str) -> Labeled[T]:
        return mint_merchant_signed(MINT, value, self.ref, path, self.hash)

    def product(self, sku: str) -> Product:
        for p in self.manifest.catalog:
            if p.sku == sku:
                return p
        raise ManifestError(f"sku {sku!r} is not in {self.merchant_id}'s signed catalog")

    # MERCHANT_SIGNED accessors for every field a contract reads.
    def payee(self) -> Labeled[str]:
        return self._field(self.manifest.paypal_merchant_id, "paypal_merchant_id")

    def sku(self, sku: str) -> Labeled[str]:
        return self._field(self.product(sku).sku, f"catalog[{sku}].sku")

    def unit_price(self, sku: str) -> Labeled[Decimal]:
        return self._field(parse_amount(self.product(sku).price), f"catalog[{sku}].price")

    def attributes(self, sku: str) -> Labeled[dict[str, str]]:
        return self._field(dict(self.product(sku).attributes), f"catalog[{sku}].attributes")

    def currency(self) -> Labeled[str]:
        return self._field(self.manifest.currency, "currency")

    def shipping_flat(self) -> Labeled[Decimal]:
        return self._field(parse_amount(self.manifest.shipping_flat), "shipping_flat")

    def tax_rate(self) -> Labeled[Decimal]:
        return self._field(parse_amount(self.manifest.tax_rate), "tax_rate")


class MerchantKeyRegistry:
    """merchant_id -> public keys registered at onboarding."""

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Ed25519PublicKey]] = {}

    def register(self, merchant_id: str, key_id: str, key: Ed25519PublicKey) -> None:
        self._keys.setdefault(merchant_id, {})[key_id] = key

    def keys_for(self, merchant_id: str) -> dict[str, Ed25519PublicKey]:
        return dict(self._keys.get(merchant_id, {}))


def verify_manifest(
    envelope: SignedEnvelope, *, merchant_id: str, registry: MerchantKeyRegistry
) -> VerifiedManifest:
    """Verify against the keys registered for `merchant_id` only.

    A manifest signed by another registered merchant (for example the attacker shop) fails here,
    because only the expected merchant's keys are consulted.
    """
    keys = registry.keys_for(merchant_id)
    if not keys:
        raise ManifestError(f"no registered keys for merchant {merchant_id!r}")
    try:
        verify(envelope, PURPOSE, keys)
    except SignatureError as e:
        raise ManifestError(f"signature: {e}") from e
    try:
        manifest = MerchantManifest.model_validate(envelope.payload)
    except ValueError as e:
        raise ManifestError(f"invalid manifest: {e}") from e
    if manifest.merchant_id != merchant_id:
        raise ManifestError("manifest names a different merchant than the one fetched")
    return VerifiedManifest(manifest, envelope)


def manifest_payload(manifest: MerchantManifest) -> dict[str, Any]:
    return manifest.model_dump(mode="json")
