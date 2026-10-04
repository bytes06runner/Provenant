"""Field contracts: the gate every PayPal order must pass (CLAUDE.md section 4.4).

  payee             MERCHANT_SIGNED from the selected merchant's manifest;
                    merchant must be in the mandate allowlist if one is set
  item.sku          MERCHANT_SIGNED from that manifest; required attributes met, none forbidden
  unit_price        MERCHANT_SIGNED, that sku's signed price; <= max_unit_price
  quantity          USER, from this mandate; == mandate quantity
  amount.total      DERIVED only from that signed price x USER quantity (+ signed shipping
                    and tax); recomputed exactly; <= max_total
  shipping_address  USER, the vault entry the mandate names; == that entry
  currency          USER or MERCHANT_SIGNED; mandate and manifest must agree

Any UNTRUSTED label, or any UNTRUSTED source, in one of these fields is a hard block whatever
the value is: provenance wins over content. The checker never stops at the first problem; it
returns every violation with the provenance that caused it, for the Provenance Graph and the
Flight Recorder.

This module is pure and deterministic: no I/O, no LLM, no PayPal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from lineage.labels import Label, Labeled
from lineage.mandate import VerifiedMandate
from lineage.manifest import ManifestError, VerifiedManifest
from lineage.vault import Address, AddressVault, VaultError

CENT = Decimal("0.01")


class Field(StrEnum):
    PAYEE = "payee"
    SKU = "item.sku"
    UNIT_PRICE = "unit_price"
    QUANTITY = "quantity"
    AMOUNT_TOTAL = "amount.total"
    SHIPPING_ADDRESS = "shipping_address"
    CURRENCY = "currency"
    MANDATE = "mandate"


class Rule(StrEnum):
    UNTRUSTED = "untrusted_in_authority_field"
    WRONG_LABEL = "wrong_label"
    WRONG_SOURCE = "wrong_source"
    VALUE_MISMATCH = "value_mismatch"
    NOT_IN_CATALOG = "not_in_catalog"
    MERCHANT_NOT_ALLOWED = "merchant_not_allowed"
    REQUIRED_ATTRIBUTE_UNMET = "required_attribute_unmet"
    FORBIDDEN_ATTRIBUTE_PRESENT = "forbidden_attribute_present"
    OVER_MAX_UNIT_PRICE = "over_max_unit_price"
    OVER_MAX_TOTAL = "over_max_total"
    CURRENCY_DISAGREES = "currency_disagrees"
    MANDATE_EXPIRED = "mandate_expired"


@dataclass(frozen=True)
class ProposedCheckout:
    """What the plan wants to buy. Every authority-bearing field is labeled."""

    payee: Labeled[str]
    sku: Labeled[str]
    unit_price: Labeled[Decimal]
    quantity: Labeled[int]
    amount_total: Labeled[Decimal]
    shipping_address: Labeled[Address]
    currency: Labeled[str]

    def fields(self) -> dict[Field, Labeled[Any]]:
        return {
            Field.PAYEE: self.payee,
            Field.SKU: self.sku,
            Field.UNIT_PRICE: self.unit_price,
            Field.QUANTITY: self.quantity,
            Field.AMOUNT_TOTAL: self.amount_total,
            Field.SHIPPING_ADDRESS: self.shipping_address,
            Field.CURRENCY: self.currency,
        }


@dataclass(frozen=True)
class Violation:
    field: Field
    rule: Rule
    detail: str
    label: Label | None = None
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractResult:
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return not self.violations

    def rules_for(self, f: Field) -> set[Rule]:
        return {v.rule for v in self.violations if v.field is f}

    def blocked_fields(self) -> set[Field]:
        return {v.field for v in self.violations}


def compute_total(
    unit_price: Decimal, quantity: int, shipping_flat: Decimal, tax_rate: Decimal
) -> Decimal:
    """The one definition of an order total. Tax is on items, rounded half-up to the cent."""
    items = (unit_price * quantity).quantize(CENT, ROUND_HALF_UP)
    tax = (items * tax_rate).quantize(CENT, ROUND_HALF_UP)
    return items + tax + shipping_flat.quantize(CENT, ROUND_HALF_UP)


class _Checker:
    def __init__(
        self,
        checkout: ProposedCheckout,
        mandate: VerifiedMandate,
        manifest: VerifiedManifest,
        vault: AddressVault,
    ) -> None:
        self.c = checkout
        self.mandate = mandate
        self.m = mandate.mandate
        self.manifest = manifest
        self.vault = vault
        self.violations: list[Violation] = []

    # ---- helpers -------------------------------------------------------------

    def flag(self, f: Field, rule: Rule, detail: str, value: Labeled[Any] | None = None) -> None:
        self.violations.append(
            Violation(
                f,
                rule,
                detail,
                value.label if value is not None else None,
                tuple(value.provenance()) if value is not None else (),
            )
        )

    def trusted(self, f: Field, value: Labeled[Any]) -> bool:
        """Hard block on UNTRUSTED anywhere in the value's provenance."""
        if value.label is Label.UNTRUSTED or any(s.kind is Label.UNTRUSTED for s in value.sources):
            self.flag(f, Rule.UNTRUSTED, "untrusted content cannot bind this field", value)
            return False
        return True

    def only_from(self, f: Field, value: Labeled[Any], allowed: Iterable[tuple[str, str]]) -> bool:
        """Every source must be one of the allowed (ref, path) pairs."""
        allowed_set = set(allowed)
        stray = [s.describe() for s in value.sources if (s.ref, s.path) not in allowed_set]
        if stray:
            self.flag(f, Rule.WRONG_SOURCE, f"not traceable to the allowed origin: {stray}", value)
            return False
        return True

    def label_is(self, f: Field, value: Labeled[Any], *labels: Label) -> bool:
        if value.label not in labels:
            want = " or ".join(lbl.name for lbl in labels)
            self.flag(f, Rule.WRONG_LABEL, f"is {value.label.name}, must be {want}", value)
            return False
        return True

    # ---- contracts -----------------------------------------------------------

    def check_payee(self) -> None:
        v, mf = self.c.payee, self.manifest
        if not self.trusted(Field.PAYEE, v):
            return
        self.label_is(Field.PAYEE, v, Label.MERCHANT_SIGNED)
        self.only_from(Field.PAYEE, v, [(mf.ref, "paypal_merchant_id")])
        if v.value != mf.manifest.paypal_merchant_id:
            self.flag(Field.PAYEE, Rule.VALUE_MISMATCH, "payee is not the signed payee", v)
        allow = self.m.merchant_allowlist
        if allow is not None and mf.merchant_id not in allow:
            self.flag(
                Field.PAYEE,
                Rule.MERCHANT_NOT_ALLOWED,
                f"merchant {mf.merchant_id!r} is not in the mandate allowlist",
                v,
            )

    def check_sku(self) -> str | None:
        """Returns the sku if it is in the signed catalog (for later checks), else None."""
        v, mf = self.c.sku, self.manifest
        if not self.trusted(Field.SKU, v):
            return None
        self.label_is(Field.SKU, v, Label.MERCHANT_SIGNED)
        try:
            product = mf.product(v.value)
        except ManifestError:
            self.flag(Field.SKU, Rule.NOT_IN_CATALOG, f"{v.value!r} is not in the catalog", v)
            return None
        self.only_from(Field.SKU, v, [(mf.ref, f"catalog[{product.sku}].sku")])
        attrs = product.attributes
        for key, want in self.m.required_attributes.items():
            if attrs.get(key) != want:
                self.flag(
                    Field.SKU,
                    Rule.REQUIRED_ATTRIBUTE_UNMET,
                    f"{key}={attrs.get(key)!r}, mandate requires {want!r}",
                    v,
                )
        for key, banned in self.m.forbidden_attributes.items():
            if attrs.get(key) in banned:
                self.flag(
                    Field.SKU,
                    Rule.FORBIDDEN_ATTRIBUTE_PRESENT,
                    f"{key}={attrs[key]!r} is forbidden by the mandate",
                    v,
                )
        return product.sku

    def check_unit_price(self, sku: str | None) -> None:
        v, mf = self.c.unit_price, self.manifest
        if not self.trusted(Field.UNIT_PRICE, v):
            return
        self.label_is(Field.UNIT_PRICE, v, Label.MERCHANT_SIGNED)
        if sku is not None:
            self.only_from(Field.UNIT_PRICE, v, [(mf.ref, f"catalog[{sku}].price")])
            if v.value != mf.unit_price(sku).value:
                self.flag(
                    Field.UNIT_PRICE, Rule.VALUE_MISMATCH, "not the signed price for this sku", v
                )
        cap = self.mandate.max_unit_price().value
        if v.value > cap:
            self.flag(Field.UNIT_PRICE, Rule.OVER_MAX_UNIT_PRICE, f"{v.value} > {cap}", v)

    def check_quantity(self) -> None:
        v = self.c.quantity
        if not self.trusted(Field.QUANTITY, v):
            return
        self.label_is(Field.QUANTITY, v, Label.USER)
        self.only_from(Field.QUANTITY, v, [(self.mandate.ref, "quantity")])
        if v.value != self.m.quantity:
            self.flag(
                Field.QUANTITY,
                Rule.VALUE_MISMATCH,
                f"{v.value} != mandate quantity {self.m.quantity}",
                v,
            )

    def check_amount_total(self, sku: str | None) -> None:
        v, mf = self.c.amount_total, self.manifest
        if not self.trusted(Field.AMOUNT_TOTAL, v):
            return
        self.label_is(Field.AMOUNT_TOTAL, v, Label.DERIVED)
        if sku is not None:
            self.only_from(
                Field.AMOUNT_TOTAL,
                v,
                [
                    (mf.ref, f"catalog[{sku}].price"),
                    (mf.ref, "shipping_flat"),
                    (mf.ref, "tax_rate"),
                    (self.mandate.ref, "quantity"),
                ],
            )
            expected = compute_total(
                mf.unit_price(sku).value,
                self.m.quantity,
                mf.shipping_flat().value,
                mf.tax_rate().value,
            )
            if v.value != expected:
                self.flag(
                    Field.AMOUNT_TOTAL,
                    Rule.VALUE_MISMATCH,
                    f"{v.value} != recomputed {expected}",
                    v,
                )
        cap = self.mandate.max_total().value
        if v.value > cap:
            self.flag(Field.AMOUNT_TOTAL, Rule.OVER_MAX_TOTAL, f"{v.value} > {cap}", v)

    def check_shipping_address(self) -> None:
        v = self.c.shipping_address
        if not self.trusted(Field.SHIPPING_ADDRESS, v):
            return
        self.label_is(Field.SHIPPING_ADDRESS, v, Label.USER)
        ref = AddressVault.source_ref(self.m.user_id, self.m.ship_to_ref)
        self.only_from(Field.SHIPPING_ADDRESS, v, [(ref, "")])
        try:
            expected = self.vault.lookup(self.m.user_id, self.m.ship_to_ref).value
        except VaultError:
            self.flag(
                Field.SHIPPING_ADDRESS,
                Rule.VALUE_MISMATCH,
                f"no confirmed vault entry {self.m.ship_to_ref!r}",
                v,
            )
            return
        if v.value != expected:
            self.flag(
                Field.SHIPPING_ADDRESS,
                Rule.VALUE_MISMATCH,
                f"is not vault entry {self.m.ship_to_ref!r}",
                v,
            )

    def check_currency(self) -> None:
        v, mf = self.c.currency, self.manifest
        if not self.trusted(Field.CURRENCY, v):
            return
        self.label_is(Field.CURRENCY, v, Label.USER, Label.MERCHANT_SIGNED)
        self.only_from(Field.CURRENCY, v, [(self.mandate.ref, "currency"), (mf.ref, "currency")])
        agreed = {v.value, self.m.currency, mf.manifest.currency}
        if len(agreed) != 1:
            self.flag(
                Field.CURRENCY,
                Rule.CURRENCY_DISAGREES,
                f"checkout {v.value}, mandate {self.m.currency}, merchant {mf.manifest.currency}",
                v,
            )

    def run(self, now: datetime) -> ContractResult:
        if now >= self.m.expires_at:
            self.flag(Field.MANDATE, Rule.MANDATE_EXPIRED, f"expired at {self.m.expires_at}")
        self.check_payee()
        sku = self.check_sku()
        self.check_unit_price(sku)
        self.check_quantity()
        self.check_amount_total(sku)
        self.check_shipping_address()
        self.check_currency()
        return ContractResult(tuple(self.violations))


def check_checkout(
    checkout: ProposedCheckout,
    *,
    mandate: VerifiedMandate,
    manifest: VerifiedManifest,
    vault: AddressVault,
    now: datetime,
) -> ContractResult:
    """Evaluate every field contract. `allowed` is True only with zero violations."""
    return _Checker(checkout, mandate, manifest, vault).run(now)
