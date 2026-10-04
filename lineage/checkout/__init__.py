"""Checkout builder: turns a contract-approved checkout into a PayPal Orders v2 request body.

It accepts only a sealed, allowed ContractResult from `check_checkout`, and every authority-
bearing value in the body comes from that result's labeled fields. Nothing here is read from
pages, LLM output or caller-supplied amounts. The amount breakdown is recomputed from the signed
manifest and must equal the approved total exactly.

The shipping address is sent with `shipping_preference: SET_PROVIDED_ADDRESS`, so the address the
contracts approved is the one PayPal ships to; it cannot be swapped during approval.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lineage.contracts import CENT, ContractResult
from lineage.manifest import VerifiedManifest
from lineage.money import format_amount

MAX_ID_LENGTH = 127  # PayPal limit for custom_id and invoice_id


class CheckoutError(Exception):
    pass


def _money(value: Decimal, currency: str) -> dict[str, str]:
    return {"currency_code": currency, "value": format_amount(value)}


def build_order_request(
    result: ContractResult,
    *,
    manifest: VerifiedManifest,
    custom_id: str,
    invoice_id: str,
    experience_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not result.sealed or result.checkout is None:
        raise CheckoutError("result was not produced by the contract checker")
    if not result.allowed:
        blocked = sorted(f.value for f in result.blocked_fields())
        raise CheckoutError(f"contracts blocked this checkout: {blocked}")
    if manifest.hash != result.manifest_hash:
        raise CheckoutError("manifest differs from the one the contracts approved")
    for name, value in (("custom_id", custom_id), ("invoice_id", invoice_id)):
        if not value or len(value) > MAX_ID_LENGTH:
            raise CheckoutError(f"{name} must be 1 to {MAX_ID_LENGTH} characters")

    c = result.checkout
    currency = c.currency.value
    sku = c.sku.value
    quantity = c.quantity.value
    unit = c.unit_price.value
    item_total = (unit * quantity).quantize(CENT, ROUND_HALF_UP)
    tax = (item_total * manifest.tax_rate().value).quantize(CENT, ROUND_HALF_UP)
    shipping = manifest.shipping_flat().value.quantize(CENT, ROUND_HALF_UP)
    if item_total + tax + shipping != c.amount_total.value:
        raise CheckoutError("breakdown does not add up to the approved total")

    address = c.shipping_address.value
    ship_to: dict[str, Any] = {
        "address_line_1": address.address_line_1,
        "admin_area_2": address.admin_area_2,
        "country_code": address.country_code,
    }
    for key in ("address_line_2", "admin_area_1", "postal_code"):
        if getattr(address, key):
            ship_to[key] = getattr(address, key)

    experience = dict(experience_context or {})
    experience["shipping_preference"] = "SET_PROVIDED_ADDRESS"

    return {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "reference_id": "pu-1",
                "custom_id": custom_id,
                "invoice_id": invoice_id,
                "payee": {"merchant_id": c.payee.value},
                "amount": {
                    **_money(c.amount_total.value, currency),
                    "breakdown": {
                        "item_total": _money(item_total, currency),
                        "shipping": _money(shipping, currency),
                        "tax_total": _money(tax, currency),
                    },
                },
                "items": [
                    {
                        "name": manifest.product(sku).title,
                        "sku": sku,
                        "quantity": str(quantity),
                        "unit_amount": _money(unit, currency),
                        "category": "PHYSICAL_GOODS",
                    }
                ],
                "shipping": {
                    "type": "SHIPPING",
                    "name": {"full_name": address.full_name},
                    "address": ship_to,
                },
            }
        ],
        "payment_source": {"paypal": {"experience_context": experience}},
    }
