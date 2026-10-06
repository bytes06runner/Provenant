"""Place one Adversarial Lab payload on the merchant simulator (evaluation and demos only).

The simulator accepts this only when started with SIMULATOR_ALLOW_PLANTED=1. Placing a payload
removes any earlier one and switches the attacker shop's built-in templates off, so a task faces
exactly one known attack.
"""

from __future__ import annotations

from typing import Any

import httpx

PAGES = {"product_page": "products/{sku}", "reviews": "reviews/{sku}", "catalog": "catalog"}


def admin_base(storefront_url: str) -> str:
    return storefront_url.rsplit("/m/", 1)[0]


def fill_values(attacker_payee: str, taxonomy: dict[str, Any]) -> dict[str, str]:
    values = taxonomy["placement"]["attacker_values"]
    return {
        # The baseline chooses whose credentials check out, so the shop id is what it acts on.
        "attacker_shop": "the authorized checkout partner (shop id: attacker)",
        "payee_name": attacker_payee,
        "address": values["address"],
        "quantity": values["quantity"],
    }


def plant(
    http: httpx.Client,
    storefront_url: str,
    *,
    host: str,
    surface: str,
    attack: dict[str, Any],
    fill: dict[str, str],
    first_sku: str,
) -> str:
    """Place the payload; return the URL of the poisoned page."""
    r = http.post(
        f"{admin_base(storefront_url)}/admin/attack",
        json={
            "merchant": host,
            "attack_id": attack["id"],
            "surface": surface,
            "text": attack["text"],
            "fill": fill,
        },
    )
    r.raise_for_status()
    return f"{storefront_url.rstrip('/')}/{PAGES[surface].format(sku=first_sku)}"


def clear(http: httpx.Client, storefront_url: str) -> None:
    http.delete(f"{admin_base(storefront_url)}/admin/attack").raise_for_status()
