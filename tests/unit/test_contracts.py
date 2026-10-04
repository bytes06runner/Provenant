"""Field contracts (CLAUDE.md 4.4): the benign path passes, every hijack class is blocked, and
an UNTRUSTED value in any authority-bearing field hard-blocks regardless of its content."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lineage.contracts import (
    Field,
    ProposedCheckout,
    Rule,
    check_checkout,
    compute_total,
)
from lineage.labels import Label, Labeled, derive, untrusted
from lineage.mandate import VerifiedMandate
from lineage.manifest import VerifiedManifest

from .lineage_world import ELSEWHERE, NOW, USER, World

SKU = "TRAIL-BLK-10"
PAGE = "page:https://attacker.example/products/trail"

ATTR = {
    Field.PAYEE: "payee",
    Field.SKU: "sku",
    Field.UNIT_PRICE: "unit_price",
    Field.QUANTITY: "quantity",
    Field.AMOUNT_TOTAL: "amount_total",
    Field.SHIPPING_ADDRESS: "shipping_address",
    Field.CURRENCY: "currency",
}


@pytest.fixture
def world() -> World:
    return World()


def labeled_total(mf: VerifiedManifest, vm: VerifiedMandate, sku: str) -> Labeled[Decimal]:
    return derive(
        compute_total, mf.unit_price(sku), vm.quantity(), mf.shipping_flat(), mf.tax_rate()
    )


def honest(world: World, vm: VerifiedMandate, mf: VerifiedManifest, sku: str = SKU):
    return ProposedCheckout(
        payee=mf.payee(),
        sku=mf.sku(sku),
        unit_price=mf.unit_price(sku),
        quantity=vm.quantity(),
        amount_total=labeled_total(mf, vm, sku),
        shipping_address=world.vault.lookup(USER, vm.mandate.ship_to_ref),
        currency=vm.currency(),
    )


def run(world, checkout, vm, mf, now=NOW):
    return check_checkout(checkout, mandate=vm, manifest=mf, vault=world.vault, now=now)


def with_field(checkout: ProposedCheckout, f: Field, value: Labeled[Any]) -> ProposedCheckout:
    return dataclasses.replace(checkout, **{ATTR[f]: value})


# ---- benign -------------------------------------------------------------------


def test_benign_checkout_passes(world):
    vm, mf = world.mandate(), world.manifest()
    result = run(world, honest(world, vm, mf), vm, mf)
    assert result.allowed, result.violations


def test_total_is_derived_from_exactly_the_signed_and_user_sources(world):
    vm, mf = world.mandate(), world.manifest()
    total = labeled_total(mf, vm, SKU)
    assert total.label is Label.DERIVED
    assert total.value == Decimal("104.00")  # 99.00 x 1 + 5.00 shipping
    assert total.source_refs() == {mf.ref, vm.ref}


def test_currency_may_come_from_the_signed_manifest(world):
    vm, mf = world.mandate(), world.manifest()
    assert run(
        world, dataclasses.replace(honest(world, vm, mf), currency=mf.currency()), vm, mf
    ).allowed


def test_tax_rounding_is_half_up_to_the_cent():
    assert compute_total(Decimal("0.05"), 1, Decimal("0"), Decimal("0.10")) == Decimal("0.06")
    assert compute_total(Decimal("19.99"), 3, Decimal("4.50"), Decimal("0.0825")) == Decimal(
        "69.42"
    )


# ---- the hard rule: UNTRUSTED in any authority field blocks, whatever the value ----


@pytest.mark.parametrize("f", list(ATTR))
def test_untrusted_copy_of_the_correct_value_is_blocked(world, f):
    """Same value as the honest checkout, but sourced from a page: must hard-block."""
    vm, mf = world.mandate(), world.manifest()
    good = honest(world, vm, mf)
    bad = with_field(good, f, untrusted(getattr(good, ATTR[f]).value, PAGE))
    result = run(world, bad, vm, mf)
    assert not result.allowed
    assert result.rules_for(f) == {Rule.UNTRUSTED}
    assert result.blocked_fields() == {f}, "only the poisoned field should be blocked"
    (v,) = [v for v in result.violations if v.field is f]
    assert v.label is Label.UNTRUSTED and any(PAGE in p for p in v.provenance)


@pytest.mark.parametrize("f", list(ATTR))
def test_value_derived_with_any_untrusted_input_is_blocked(world, f):
    """Mixing one page value into an otherwise trusted computation taints the result."""
    vm, mf = world.mandate(), world.manifest()
    good = honest(world, vm, mf)
    honest_value = getattr(good, ATTR[f])
    tainted = derive(lambda v, _hint: v, honest_value, untrusted("ignored", PAGE))
    result = run(world, with_field(good, f, tainted), vm, mf)
    assert Rule.UNTRUSTED in result.rules_for(f)


@settings(max_examples=60, deadline=None)
@given(
    fields=st.sets(st.sampled_from(list(ATTR)), min_size=1),
    junk=st.one_of(st.text(max_size=12), st.integers(-5, 500), st.decimals(0, 1000, places=2)),
)
def test_any_set_of_untrusted_fields_blocks_each_of_them(fields, junk):
    world = World()
    vm, mf = world.mandate(), world.manifest()
    checkout = honest(world, vm, mf)
    for f in fields:
        checkout = with_field(checkout, f, untrusted(junk, PAGE))
    result = run(world, checkout, vm, mf)
    assert not result.allowed
    for f in fields:
        assert Rule.UNTRUSTED in result.rules_for(f)


# ---- hijack classes ---------------------------------------------------------------


def test_payee_swap_with_attackers_own_signed_payee_is_blocked(world):
    """The attacker shop has a valid signed manifest; its payee still cannot bind this order."""
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    result = run(world, with_field(honest(world, vm, mf), Field.PAYEE, attacker.payee()), vm, mf)
    assert result.rules_for(Field.PAYEE) == {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH}


def test_payee_from_wrong_signed_field_is_blocked(world):
    """A signed value from the right manifest but the wrong field (a sku) is not a payee."""
    vm, mf = world.mandate(), world.manifest()
    result = run(world, with_field(honest(world, vm, mf), Field.PAYEE, mf.sku(SKU)), vm, mf)
    assert {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH} <= result.rules_for(Field.PAYEE)


def test_merchant_outside_allowlist_is_blocked(world):
    vm, mf = world.mandate(merchant_allowlist=["kestrel"]), world.manifest()
    result = run(world, honest(world, vm, mf), vm, mf)
    assert result.rules_for(Field.PAYEE) == {Rule.MERCHANT_NOT_ALLOWED}


def test_merchant_inside_allowlist_passes(world):
    vm, mf = world.mandate(merchant_allowlist=["northwind"]), world.manifest()
    assert run(world, honest(world, vm, mf), vm, mf).allowed


def test_price_inflation_via_signed_price_of_another_sku_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    inflated = mf.unit_price("TRAIL-BLK-10-LTH")  # 110.00, signed, but for a different sku
    result = run(world, with_field(honest(world, vm, mf), Field.UNIT_PRICE, inflated), vm, mf)
    assert result.rules_for(Field.UNIT_PRICE) == {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH}


def test_price_from_attacker_manifest_is_blocked(world):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    result = run(
        world,
        with_field(honest(world, vm, mf), Field.UNIT_PRICE, attacker.unit_price(SKU)),
        vm,
        mf,
    )
    assert Rule.WRONG_SOURCE in result.rules_for(Field.UNIT_PRICE)


def test_item_swap_to_wrong_color_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    result = run(world, honest(world, vm, mf, "TRAIL-NVY-10"), vm, mf)
    assert result.rules_for(Field.SKU) == {Rule.REQUIRED_ATTRIBUTE_UNMET}


def test_forbidden_attribute_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    result = run(world, honest(world, vm, mf, "TRAIL-BLK-10-LTH"), vm, mf)
    assert result.rules_for(Field.SKU) == {Rule.FORBIDDEN_ATTRIBUTE_PRESENT}


def test_unit_price_over_mandate_cap_is_blocked(world):
    vm, mf = world.mandate(max_total="200.00"), world.manifest()
    result = run(world, honest(world, vm, mf, "TRAIL-BLK-10-PRO"), vm, mf)  # 125 > 120
    assert result.rules_for(Field.UNIT_PRICE) == {Rule.OVER_MAX_UNIT_PRICE}


def test_sku_not_in_signed_catalog_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    fake = world.manifest(
        "attacker",
        catalog=[
            {
                "sku": "GHOST-1",
                "title": "Ghost",
                "attributes": {"color": "black", "size_us": "10"},
                "price": "1.00",
                "currency": "USD",
                "stock": 1,
            }
        ],
    ).sku("GHOST-1")
    result = run(world, with_field(honest(world, vm, mf), Field.SKU, fake), vm, mf)
    assert Rule.NOT_IN_CATALOG in result.rules_for(Field.SKU)


def test_sku_signed_by_attacker_for_same_name_is_blocked(world):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    result = run(world, with_field(honest(world, vm, mf), Field.SKU, attacker.sku(SKU)), vm, mf)
    assert Rule.WRONG_SOURCE in result.rules_for(Field.SKU)


def test_quantity_bump_by_computation_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    doubled = derive(lambda q: q * 2, vm.quantity())
    result = run(world, with_field(honest(world, vm, mf), Field.QUANTITY, doubled), vm, mf)
    assert result.rules_for(Field.QUANTITY) == {Rule.WRONG_LABEL, Rule.VALUE_MISMATCH}


def test_quantity_from_another_mandate_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    other = world.mandate(
        mandate_id="m-2", nonce="n-fedcba9876543210", quantity=3, max_total="400.00"
    )
    result = run(world, with_field(honest(world, vm, mf), Field.QUANTITY, other.quantity()), vm, mf)
    assert result.rules_for(Field.QUANTITY) == {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH}


def test_quantity_must_match_mandate_even_for_larger_order(world):
    vm, mf = world.mandate(quantity=2, max_total="300.00"), world.manifest()
    assert run(world, honest(world, vm, mf), vm, mf).allowed


def test_address_swap_to_another_vault_entry_is_blocked(world):
    """USER-labeled, but not the entry this mandate names: exfiltration to a drop point."""
    vm, mf = world.mandate(), world.manifest()
    office = world.vault.lookup(USER, "office")
    assert office.value == ELSEWHERE
    result = run(world, with_field(honest(world, vm, mf), Field.SHIPPING_ADDRESS, office), vm, mf)
    assert result.rules_for(Field.SHIPPING_ADDRESS) == {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH}


def test_total_tampered_by_one_cent_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    bumped = derive(
        lambda p, q, s, t: compute_total(p, q, s, t) + Decimal("0.01"),
        mf.unit_price(SKU),
        vm.quantity(),
        mf.shipping_flat(),
        mf.tax_rate(),
    )
    result = run(world, with_field(honest(world, vm, mf), Field.AMOUNT_TOTAL, bumped), vm, mf)
    assert result.rules_for(Field.AMOUNT_TOTAL) == {Rule.VALUE_MISMATCH}


def test_total_copied_from_mandate_cap_is_blocked(world):
    """A USER value is not a computed total: label must be DERIVED."""
    vm, mf = world.mandate(), world.manifest()
    result = run(
        world, with_field(honest(world, vm, mf), Field.AMOUNT_TOTAL, vm.max_total()), vm, mf
    )
    assert {Rule.WRONG_LABEL, Rule.WRONG_SOURCE} <= result.rules_for(Field.AMOUNT_TOTAL)


def test_total_derived_from_other_sku_price_is_blocked(world):
    vm, mf = world.mandate(), world.manifest()
    total = labeled_total(mf, vm, "TRAIL-NVY-10")
    result = run(world, with_field(honest(world, vm, mf), Field.AMOUNT_TOTAL, total), vm, mf)
    assert {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH} <= result.rules_for(Field.AMOUNT_TOTAL)


def test_total_over_mandate_cap_is_blocked(world):
    vm, mf = world.mandate(quantity=2), world.manifest()  # 2 x 99 + 5 = 203 > 130
    result = run(world, honest(world, vm, mf), vm, mf)
    assert result.rules_for(Field.AMOUNT_TOTAL) == {Rule.OVER_MAX_TOTAL}


def test_currency_disagreement_is_blocked(world):
    vm = world.mandate(currency="EUR")
    mf = world.manifest()
    result = run(world, honest(world, vm, mf), vm, mf)
    assert result.rules_for(Field.CURRENCY) == {Rule.CURRENCY_DISAGREES}


def test_expired_mandate_blocks_at_check_time(world):
    vm, mf = world.mandate(), world.manifest()
    result = run(world, honest(world, vm, mf), vm, mf, now=NOW + timedelta(hours=2))
    assert result.rules_for(Field.MANDATE) == {Rule.MANDATE_EXPIRED}


def test_all_violations_are_reported_not_just_the_first(world):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    bad = dataclasses.replace(
        honest(world, vm, mf),
        payee=attacker.payee(),
        quantity=untrusted(5, PAGE),
        shipping_address=untrusted(ELSEWHERE, PAGE),
    )
    result = run(world, bad, vm, mf)
    # The attacker payee also unbinds the honest sku, price and total (different manifest).
    assert result.blocked_fields() == {
        Field.PAYEE,
        Field.QUANTITY,
        Field.SHIPPING_ADDRESS,
        Field.SKU,
        Field.UNIT_PRICE,
        Field.AMOUNT_TOTAL,
    }


def test_violations_carry_the_provenance_path(world):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    result = run(world, with_field(honest(world, vm, mf), Field.PAYEE, attacker.payee()), vm, mf)
    paths = {p for v in result.violations for p in v.provenance}
    assert f"MERCHANT_SIGNED:{attacker.ref}#paypal_merchant_id" in paths


def test_checkout_exposes_every_authority_field(world):
    vm, mf = world.mandate(), world.manifest()
    assert set(honest(world, vm, mf).fields()) == set(ATTR)


def test_mandate_naming_a_missing_vault_entry_is_blocked(world):
    vm, mf = world.mandate(ship_to_ref="cabin"), world.manifest()
    checkout = dataclasses.replace(
        honest(world, world.mandate(), mf), shipping_address=world.vault.lookup(USER, "home")
    )
    result = run(
        world,
        dataclasses.replace(
            checkout,
            quantity=vm.quantity(),
            amount_total=labeled_total(mf, vm, SKU),
            currency=vm.currency(),
        ),
        vm,
        mf,
    )
    assert result.rules_for(Field.SHIPPING_ADDRESS) == {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH}


# ---- cross-field binding: sku, price and total must share the payee's manifest -------


def attacker_total(attacker: VerifiedManifest, vm: VerifiedMandate) -> Labeled[Decimal]:
    return derive(
        compute_total,
        attacker.unit_price(SKU),
        vm.quantity(),
        attacker.shipping_flat(),
        attacker.tax_rate(),
    )


@pytest.mark.parametrize("f", [Field.SKU, Field.UNIT_PRICE, Field.AMOUNT_TOTAL])
def test_attacker_signed_field_with_legit_payee_is_blocked(world, f):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    poisoned = {
        Field.SKU: attacker.sku(SKU),
        Field.UNIT_PRICE: attacker.unit_price(SKU),
        Field.AMOUNT_TOTAL: attacker_total(attacker, vm),
    }[f]
    result = run(world, with_field(honest(world, vm, mf), f, poisoned), vm, mf)
    assert Rule.MANIFEST_DIFFERS_FROM_PAYEE in result.rules_for(f)
    assert Field.PAYEE not in result.blocked_fields()


def test_attacker_price_sku_and_total_together_with_legit_payee_are_blocked(world):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    bad = dataclasses.replace(
        honest(world, vm, mf),
        sku=attacker.sku(SKU),
        unit_price=attacker.unit_price(SKU),
        amount_total=attacker_total(attacker, vm),
    )
    result = run(world, bad, vm, mf)
    for f in (Field.SKU, Field.UNIT_PRICE, Field.AMOUNT_TOTAL):
        assert Rule.MANIFEST_DIFFERS_FROM_PAYEE in result.rules_for(f)


def test_attacker_payee_with_legit_price_sku_and_total_is_blocked(world):
    """The reverse: honest item and price, money redirected to the attacker."""
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    result = run(world, with_field(honest(world, vm, mf), Field.PAYEE, attacker.payee()), vm, mf)
    assert {Rule.WRONG_SOURCE, Rule.VALUE_MISMATCH} <= result.rules_for(Field.PAYEE)
    for f in (Field.SKU, Field.UNIT_PRICE, Field.AMOUNT_TOTAL):
        assert result.rules_for(f) == {Rule.MANIFEST_DIFFERS_FROM_PAYEE}


def test_fully_consistent_attacker_checkout_against_honest_selection_is_blocked(world):
    """Everything from the attacker's manifest, while the plan selected the honest merchant."""
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    result = run(world, honest(world, vm, attacker), vm, mf)
    assert not result.allowed
    assert Rule.WRONG_SOURCE in result.rules_for(Field.PAYEE)


def test_stale_manifest_of_same_merchant_cannot_supply_the_price(world):
    """Same merchant, older validly signed manifest (cheaper price): different hash, blocked."""
    vm = world.mandate()
    old = world.manifest(version=1)
    current = world.manifest(version=2, issued_at="2026-10-03T00:00:00Z")
    assert old.hash != current.hash
    result = run(
        world,
        with_field(honest(world, vm, current), Field.UNIT_PRICE, old.unit_price(SKU)),
        vm,
        current,
    )
    assert Rule.MANIFEST_DIFFERS_FROM_PAYEE in result.rules_for(Field.UNIT_PRICE)


def test_payee_backed_by_two_manifests_is_blocked(world):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    two = derive(lambda a, _b: a, mf.payee(), attacker.payee())
    result = run(world, with_field(honest(world, vm, mf), Field.PAYEE, two), vm, mf)
    assert Rule.MANIFEST_DIFFERS_FROM_PAYEE in result.rules_for(Field.PAYEE)


@pytest.mark.parametrize("mismatch", ["ref", "digest"])
def test_binding_checks_both_manifest_ref_and_hash(world, mismatch):
    """Defense in depth against a minting bug: a source whose ref and digest disagree with the
    payee's manifest in only one of the two must still be refused."""
    from lineage.labels import MINT, mint_merchant_signed

    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    ref, digest = mf.ref, mf.hash
    if mismatch == "ref":
        ref = attacker.ref
    else:
        digest = attacker.hash
    price = mint_merchant_signed(MINT, Decimal("99.00"), ref, f"catalog[{SKU}].price", digest)
    result = run(world, with_field(honest(world, vm, mf), Field.UNIT_PRICE, price), vm, mf)
    assert Rule.MANIFEST_DIFFERS_FROM_PAYEE in result.rules_for(Field.UNIT_PRICE)
