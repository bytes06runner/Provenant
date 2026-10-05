"""Intent Mandate: the user's confirmed, signed statement of what the agent may buy.

Flow (CLAUDE.md section 4.1): an extractor proposes fields, the user confirms or edits them,
then the confirmed mandate is JCS-canonicalized and signed with the user's Ed25519 key.

Only `verify_mandate` produces a `VerifiedMandate`, and only a VerifiedMandate can hand out
USER-labeled fields. Verification checks the signature, the user binding, the validity window,
and (when a nonce registry is given) that the mandate has not been used before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lineage.labels import MINT, Labeled, mint_user
from lineage.money import parse_amount, parse_currency
from lineage.signing import SignatureError, SignedEnvelope, sign, verify

PURPOSE = "provenant/mandate/v1"


class SelectionPreference(StrEnum):
    """How the shopper wants the agent to choose among compliant items. A soft preference:
    contracts do not enforce it, the agent's ranking should follow it, and Blackbox judges the
    purchase against it."""

    LOWEST_TOTAL = "lowest_total"
    BEST_REVIEWED = "best_reviewed"


class AutonomyMode(StrEnum):
    HUMAN_PRESENT = "human_present"  # buyer approves each payment in PayPal
    AUTONOMOUS = "autonomous"  # vaulted payment method, no per-purchase approval


class MandateError(Exception):
    """A mandate that must not be used: bad signature, wrong user, expired, replayed, invalid."""


class IntentMandate(BaseModel):
    """The confirmed mandate. Money fields are decimal strings so the signed bytes are exact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    nonce: str = Field(min_length=16, max_length=64)
    issued_at: datetime
    expires_at: datetime
    category: str = Field(min_length=1, max_length=64)
    required_attributes: dict[str, str] = Field(default_factory=dict)
    forbidden_attributes: dict[str, list[str]] = Field(default_factory=dict)
    max_unit_price: str
    max_total: str
    currency: str
    quantity: int = Field(ge=1, le=100)
    merchant_allowlist: list[str] | None = None
    ship_to_ref: str = Field(min_length=1, max_length=64)
    autonomy_mode: AutonomyMode = AutonomyMode.HUMAN_PRESENT
    preference: SelectionPreference | None = None

    @field_validator("max_unit_price", "max_total")
    @classmethod
    def _amount(cls, v: str) -> str:
        parse_amount(v)
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        return parse_currency(v)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _consistent(self) -> IntentMandate:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if parse_amount(self.max_unit_price) > parse_amount(self.max_total):
            raise ValueError("max_unit_price cannot exceed max_total")
        clash = set(self.required_attributes) & set(self.forbidden_attributes)
        for key in clash:
            if self.required_attributes[key] in self.forbidden_attributes[key]:
                raise ValueError(f"attribute {key!r} is both required and forbidden")
        return self

    def to_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        # Canonical timestamp form so the signed bytes do not depend on how it was parsed.
        data["issued_at"] = _iso(self.issued_at)
        data["expires_at"] = _iso(self.expires_at)
        return data


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NonceRegistry(Protocol):
    def claim(self, user_id: str, nonce: str) -> bool:
        """Record first use. Return False if this (user, nonce) was already claimed."""


def sign_mandate(mandate: IntentMandate, user_key: Ed25519PrivateKey) -> SignedEnvelope:
    return sign(PURPOSE, mandate.to_payload(), user_key)


@dataclass(frozen=True)
class VerifiedMandate:
    mandate: IntentMandate
    envelope: SignedEnvelope

    @property
    def ref(self) -> str:
        return f"mandate:{self.envelope.payload_hash}"

    def _field[T](self, value: T, path: str) -> Labeled[T]:
        return mint_user(MINT, value, self.ref, path, self.envelope.payload_hash)

    # USER-labeled accessors for every field a contract reads.
    def quantity(self) -> Labeled[int]:
        return self._field(self.mandate.quantity, "quantity")

    def currency(self) -> Labeled[str]:
        return self._field(self.mandate.currency, "currency")

    def max_unit_price(self) -> Labeled[Decimal]:
        return self._field(parse_amount(self.mandate.max_unit_price), "max_unit_price")

    def max_total(self) -> Labeled[Decimal]:
        return self._field(parse_amount(self.mandate.max_total), "max_total")

    def ship_to_ref(self) -> Labeled[str]:
        return self._field(self.mandate.ship_to_ref, "ship_to_ref")


def verify_mandate(
    envelope: SignedEnvelope,
    *,
    user_id: str,
    user_keys: dict[str, Ed25519PublicKey],
    now: datetime,
    nonces: NonceRegistry | None = None,
) -> VerifiedMandate:
    try:
        verify(envelope, PURPOSE, user_keys)
    except SignatureError as e:
        raise MandateError(f"signature: {e}") from e
    try:
        mandate = IntentMandate.model_validate(envelope.payload)
    except ValueError as e:
        raise MandateError(f"invalid mandate: {e}") from e
    if mandate.user_id != user_id:
        raise MandateError("mandate belongs to a different user")
    if now.tzinfo is None:
        raise MandateError("now must be timezone-aware")
    if now < mandate.issued_at:
        raise MandateError("mandate is not valid yet")
    if now >= mandate.expires_at:
        raise MandateError("mandate expired")
    if nonces is not None and not nonces.claim(mandate.user_id, mandate.nonce):
        raise MandateError("mandate nonce already used (replay)")
    return VerifiedMandate(mandate, envelope)
