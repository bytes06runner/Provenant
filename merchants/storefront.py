"""One simulated merchant: its signed manifest, its (untrusted) pages, and its fulfillment.

Behavior comes from the profile in config/merchants/<key>.yaml:
  honest           ships what it sells, signs the truth
  sloppy           ships another variant of the same model with some probability
  misrepresenting  signs false attribute values on some skus (provable from its own signature)
  attacker         pages and reviews carry prompt-injection payloads
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lineage.manifest import MerchantManifest, sign_manifest
from lineage.signing import SignedEnvelope, key_id, public_key_b64
from merchants.catalog import Product, Seed


class StorefrontError(KeyError):
    pass


@dataclass(frozen=True)
class Shipment:
    order_ref: str
    ordered_sku: str
    shipped_sku: str
    shipped_attributes: dict[str, str]

    @property
    def correct(self) -> bool:
        return self.shipped_sku == self.ordered_sku


def _unit_interval(*parts: str) -> Decimal:
    """Deterministic number in [0, 1) from the inputs (same order, same outcome)."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return Decimal(int.from_bytes(digest[:8], "big")) / Decimal(2**64)


class Storefront:
    def __init__(
        self,
        profile: dict[str, Any],
        seed: Seed,
        signing_key: Ed25519PrivateKey,
        paypal_merchant_id: str,
    ) -> None:
        self.profile = profile
        self.key = str(profile["key"])
        self.seed = seed
        self.signing_key = signing_key
        self.paypal_merchant_id = paypal_merchant_id
        self.behavior: dict[str, Any] = profile.get("behavior") or {}
        self._by_sku = {p.sku: p for p in seed.products}
        self.manifest = self._build_manifest()
        self.envelope: SignedEnvelope = sign_manifest(self.manifest, signing_key)

    # ---- manifest ----------------------------------------------------------------

    def _build_manifest(self) -> MerchantManifest:
        document = {
            "merchant_id": self.key,
            "display_name": str(self.profile["display_name"]),
            "paypal_merchant_id": self.paypal_merchant_id,
            "version": int(self.seed.created.replace("-", "")),
            "issued_at": f"{self.seed.created}T00:00:00Z",
            "currency": self.seed.currency,
            "shipping_flat": str(self.profile["shipping_flat"]),
            "tax_rate": str(self.profile["tax_rate"]),
            "catalog": [
                {
                    "sku": p.sku,
                    "title": p.title[:127],
                    "attributes": dict(p.attributes),  # what the merchant signs
                    "price": p.price,
                    "currency": self.seed.currency,
                    "stock": p.stock,
                }
                for p in self.seed.products
            ],
            "policy": self.profile["policy"],
        }
        return MerchantManifest.model_validate(document)

    def public_key(self) -> dict[str, str]:
        pub = self.signing_key.public_key()
        return {"merchant_id": self.key, "key_id": key_id(pub), "public_key": public_key_b64(pub)}

    def product(self, sku: str) -> Product:
        try:
            return self._by_sku[sku]
        except KeyError:
            raise StorefrontError(f"{self.key} has no sku {sku!r}") from None

    # ---- pages (always UNTRUSTED to Provenant) ---------------------------------------

    def _payloads(self, sku: str) -> list[str]:
        if not self.behavior.get("inject_pages"):
            return []
        p = self.product(sku)
        fill = {
            "payee": self.paypal_merchant_id,
            "quantity": "5",
            "address": "99 Exfil Rd, Reno, NV 89501",
            "price": str(Decimal(p.price) / 2),
        }
        return [t.format(**fill) for t in self.behavior.get("payload_templates", [])]

    def product_page(self, sku: str) -> str:
        p = self.product(sku)
        e = html.escape
        attrs = "".join(f"<li>{e(k)}: {e(v)}</li>" for k, v in sorted(p.attributes.items()))
        injected = "".join(
            f'<div class="promo" style="display:none">{e(x)}</div>' for x in self._payloads(sku)
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{e(p.title)} | {e(self.profile['display_name'])}</title></head><body>"
            f"<h1>{e(p.title)}</h1><p class='price'>{e(p.price)} {e(self.seed.currency)}</p>"
            f"<p>{e(p.description)}</p><ul class='specs'>{attrs}</ul>{injected}"
            f"<a href='../reviews/{e(p.sku)}'>Reviews</a></body></html>"
        )

    def reviews_page(self, sku: str) -> str:
        p = self.product(sku)
        e = html.escape
        items = [f"<li><b>{r.rating}/5</b> {e(r.text)}</li>" for r in p.reviews]
        items += [f"<li><b>5/5</b> {e(x)}</li>" for x in self._payloads(sku)]
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>Reviews: {e(p.title)}</title></head><body>"
            f"<h1>Customer reviews</h1><ul>{''.join(items)}</ul></body></html>"
        )

    # ---- fulfillment -------------------------------------------------------------------

    def fulfill(self, order_ref: str, sku: str, *, force_wrong_variant: bool = False) -> Shipment:
        """Ship an order. `force_wrong_variant` plants a fulfillment fault for evaluation; the
        service only honors it when started with SIMULATOR_ALLOW_PLANTED=1."""
        ordered = self.product(sku)
        shipped = ordered
        p_wrong = Decimal(str(self.behavior.get("wrong_variant_probability", "0")))
        if force_wrong_variant or (
            p_wrong > 0 and _unit_interval(self.key, order_ref, sku) < p_wrong
        ):
            variants = [
                q
                for q in self.seed.products
                if q.sku != sku
                and q.true_attributes.get("size_us") == ordered.true_attributes.get("size_us")
            ]
            if variants:
                pick = int(_unit_interval("variant", self.key, order_ref) * len(variants))
                shipped = variants[pick]
        return Shipment(order_ref, sku, shipped.sku, dict(shipped.true_attributes))
