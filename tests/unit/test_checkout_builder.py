"""Checkout builder: only a sealed, allowed contract result becomes a PayPal order body."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from lineage.checkout import CheckoutError, build_order_request
from lineage.contracts import ContractResult, check_checkout
from lineage.labels import untrusted

from .lineage_world import NOW, World
from .test_contracts import PAGE, SKU, honest

CUSTOM_ID = "pv:" + "a" * 32
INVOICE = "pv-inv-1"


@pytest.fixture
def world() -> World:
    return World()


def approve(world, vm, mf, checkout=None):
    checkout = checkout or honest(world, vm, mf)
    return check_checkout(checkout, mandate=vm, manifest=mf, vault=world.vault, now=NOW)


def test_body_carries_exactly_the_approved_values(world):
    vm, mf = world.mandate(), world.manifest(tax_rate="0.0825")
    result = approve(world, vm, mf)
    assert result.allowed and result.sealed
    body = build_order_request(
        result,
        manifest=mf,
        custom_id=CUSTOM_ID,
        invoice_id=INVOICE,
        experience_context={"brand_name": "Provenant", "user_action": "CONTINUE"},
    )
    pu = body["purchase_units"][0]
    assert body["intent"] == "AUTHORIZE"
    assert pu["payee"] == {"merchant_id": "NWPAYEE123"}
    assert pu["custom_id"] == CUSTOM_ID and pu["invoice_id"] == INVOICE
    assert pu["items"] == [
        {
            "name": "Trail runner, black, US 10",
            "sku": SKU,
            "quantity": "1",
            "unit_amount": {"currency_code": "USD", "value": "99.00"},
            "category": "PHYSICAL_GOODS",
        }
    ]
    # 99.00 items + 8.17 tax (8.1675 rounded half-up) + 5.00 shipping
    assert pu["amount"] == {
        "currency_code": "USD",
        "value": "112.17",
        "breakdown": {
            "item_total": {"currency_code": "USD", "value": "99.00"},
            "shipping": {"currency_code": "USD", "value": "5.00"},
            "tax_total": {"currency_code": "USD", "value": "8.17"},
        },
    }
    assert pu["shipping"]["address"] == {
        "address_line_1": "1 Main St",
        "admin_area_2": "San Jose",
        "country_code": "US",
        "admin_area_1": "CA",
        "postal_code": "95131",
    }
    ctx = body["payment_source"]["paypal"]["experience_context"]
    assert ctx["shipping_preference"] == "SET_PROVIDED_ADDRESS" and ctx["brand_name"] == "Provenant"


def test_breakdown_sums_to_total(world):
    vm, mf = world.mandate(quantity=1), world.manifest(tax_rate="0.0725", shipping_flat="7.99")
    pu = build_order_request(
        approve(world, vm, mf), manifest=mf, custom_id=CUSTOM_ID, invoice_id=INVOICE
    )["purchase_units"][0]
    b = pu["amount"]["breakdown"]
    parts = sum(Decimal(b[k]["value"]) for k in ("item_total", "shipping", "tax_total"))
    assert parts == Decimal(pu["amount"]["value"])


def test_caller_cannot_override_shipping_preference(world):
    vm, mf = world.mandate(), world.manifest()
    body = build_order_request(
        approve(world, vm, mf),
        manifest=mf,
        custom_id=CUSTOM_ID,
        invoice_id=INVOICE,
        experience_context={"shipping_preference": "GET_FROM_FILE"},
    )
    ctx = body["payment_source"]["paypal"]["experience_context"]
    assert ctx["shipping_preference"] == "SET_PROVIDED_ADDRESS"


def test_blocked_checkout_cannot_be_built(world):
    vm, mf = world.mandate(), world.manifest()
    bad = dataclasses.replace(honest(world, vm, mf), payee=untrusted("NWPAYEE123", PAGE))
    with pytest.raises(CheckoutError, match="blocked"):
        build_order_request(
            approve(world, vm, mf, bad), manifest=mf, custom_id=CUSTOM_ID, invoice_id=INVOICE
        )


def test_hand_made_result_is_refused(world):
    """A result with no violations that did not come from check_checkout is not accepted."""
    vm, mf = world.mandate(), world.manifest()
    forged = ContractResult((), honest(world, vm, mf), mf.hash, vm.envelope.payload_hash)
    with pytest.raises(CheckoutError, match="not produced"):
        build_order_request(forged, manifest=mf, custom_id=CUSTOM_ID, invoice_id=INVOICE)


def test_approved_result_cannot_be_reused_with_another_manifest(world):
    vm, mf, attacker = world.mandate(), world.manifest(), world.manifest("attacker")
    with pytest.raises(CheckoutError, match="manifest differs"):
        build_order_request(
            approve(world, vm, mf), manifest=attacker, custom_id=CUSTOM_ID, invoice_id=INVOICE
        )


@pytest.mark.parametrize(
    ("custom_id", "invoice_id"), [("", INVOICE), (CUSTOM_ID, ""), ("x" * 128, INVOICE)]
)
def test_id_limits(world, custom_id, invoice_id):
    vm, mf = world.mandate(), world.manifest()
    with pytest.raises(CheckoutError, match="characters"):
        build_order_request(
            approve(world, vm, mf), manifest=mf, custom_id=custom_id, invoice_id=invoice_id
        )


def test_breakdown_mismatch_is_refused(world):
    """Defensive: if the manifest's tax changed under an approved total, refuse to build."""
    vm, mf = world.mandate(), world.manifest()
    result = approve(world, vm, mf)
    tampered = dataclasses.replace(
        result,
        checkout=dataclasses.replace(
            result.checkout,
            amount_total=dataclasses.replace(result.checkout.amount_total, value=Decimal("1.00")),
        ),
    )
    with pytest.raises(CheckoutError, match="add up"):
        build_order_request(tampered, manifest=mf, custom_id=CUSTOM_ID, invoice_id=INVOICE)
