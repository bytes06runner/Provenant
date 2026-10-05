"""Catalog seeds for the storefront simulator.

A seed is built in two steps:
  1. `draft_products`: attributes, prices and stock drawn deterministically from the category spec
     with the merchant's seed (same spec and seed, same catalog, every time).
  2. Text (titles, descriptions, reviews) is written by an LLM in scripts/seed_catalog.py, then the
     whole seed is frozen to `merchants/seeds/<merchant>-<date>.json` and committed.

Each product keeps two attribute sets: `attributes` (what the merchant signs in its manifest) and
`true_attributes` (what actually ships). They differ only for a misrepresenting merchant, whose
false claims are therefore provable from its own signature.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from lineage.money import format_amount, parse_amount


class CatalogError(RuntimeError):
    pass


@dataclass
class Review:
    rating: int
    text: str


@dataclass
class Product:
    sku: str
    attributes: dict[str, str]  # signed in the manifest
    true_attributes: dict[str, str]  # what ships
    price: str
    stock: int
    title: str = ""
    description: str = ""
    reviews: list[Review] = field(default_factory=list)


@dataclass
class Seed:
    merchant: str
    category: str
    currency: str
    created: str  # YYYY-MM-DD
    generator: dict[str, Any]
    products: list[Product]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Seed:
        d = json.loads(text)
        products = [
            Product(**{**p, "reviews": [Review(**r) for r in p.get("reviews", [])]})
            for p in d["products"]
        ]
        return cls(**{**d, "products": products})


def draft_products(spec: dict[str, Any], profile: dict[str, Any]) -> list[Product]:
    # Seeded PRNG on purpose: reproducible demo catalogs, not security.
    rng = random.Random(int(profile["catalog_seed"]))  # noqa: S311
    attrs: dict[str, list[str]] = spec["attributes"]
    low, high = (parse_amount(x) for x in spec["price_range"])
    step = parse_amount(spec["price_step"])
    steps = int((high - low) / step)
    stock_low, stock_high = spec["stock_range"]
    count = int(profile["skus"])
    prefix = profile["key"][:3].upper()

    combos = 1
    for values in attrs.values():
        combos *= len(values)
    if count > combos:
        raise CatalogError(f"spec allows {combos} distinct products, {count} requested")

    chosen: list[dict[str, str]] = [dict(a) for a in spec.get("anchors", [])]
    seen = {tuple(sorted(a.items())) for a in chosen}
    while len(chosen) < count:
        cand = {name: rng.choice(values) for name, values in attrs.items()}
        sig = tuple(sorted(cand.items()))
        if sig not in seen:
            seen.add(sig)
            chosen.append(cand)

    products = []
    for i, true_attrs in enumerate(chosen[:count]):
        price = low + step * rng.randint(0, steps)
        stock = rng.randint(stock_low, stock_high)
        if i < len(spec.get("anchors", [])):
            stock = max(stock, 3)  # anchors are always buyable
        products.append(
            Product(
                sku=f"{prefix}-{i + 1:03d}",
                attributes=dict(true_attrs),
                true_attributes=dict(true_attrs),
                price=format_amount(price),
                stock=stock,
            )
        )
    _apply_false_claims(products, profile, rng)
    return products


def _apply_false_claims(
    products: list[Product], profile: dict[str, Any], rng: random.Random
) -> None:
    for claim in (profile.get("behavior") or {}).get("false_claims", []):
        attr, claimed = claim["attribute"], claim["claimed"]
        eligible = [p for p in products if p.true_attributes.get(attr) != claimed]
        n = int(len(products) * Decimal(claim["fraction"]))
        for p in rng.sample(eligible, min(n, len(eligible))):
            p.attributes[attr] = claimed  # signed, but not what ships


def latest_seed(seeds_dir: Path, merchant: str) -> Seed:
    files = sorted(seeds_dir.glob(f"{merchant}-*.json"))
    if not files:
        raise CatalogError(f"no seed for {merchant!r} in {seeds_dir}: run scripts/seed_catalog.py")
    seed = Seed.from_json(files[-1].read_text())
    if seed.merchant != merchant:
        raise CatalogError(f"{files[-1].name} belongs to {seed.merchant!r}")
    missing = [p.sku for p in seed.products if not p.title or not p.description]
    if missing:
        raise CatalogError(f"seed for {merchant!r} has products without text: {missing}")
    return seed
