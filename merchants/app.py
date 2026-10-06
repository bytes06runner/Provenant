"""Storefront simulator service: hosts every merchant in config/merchants/ on one FastAPI app.

    GET  /healthz
    GET  /m/{merchant}/.well-known/provenant-manifest.json   signed manifest envelope
    GET  /m/{merchant}/.well-known/provenant-keys.json       public key (for onboarding only)
    GET  /m/{merchant}/products/{sku}                         product page (untrusted HTML)
    GET  /m/{merchant}/reviews/{sku}                          reviews page (untrusted HTML)
    GET  /m/{merchant}/catalog                                catalog page (untrusted HTML)
    POST /m/{merchant}/fulfill   {"order_ref": ..., "sku": ...}  simulated shipment
    GET|POST|DELETE /admin/attack   place one Adversarial Lab payload (SIMULATOR_ALLOW_PLANTED=1)

Run: uvicorn merchants.app:app --port 8710   (after scripts/seed_catalog.py and
scripts/register_merchants.py)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from merchants.catalog import latest_seed
from merchants.keystore import load_or_create
from merchants.registry import load_records
from merchants.storefront import PlantedAttack, Storefront, StorefrontError
from paypal.config import CONFIG_DIR, REPO_ROOT

KEYS_DIR = REPO_ROOT / "var" / "keys"
REGISTRY_PATH = REPO_ROOT / "var" / "registry.json"
SEEDS_DIR = REPO_ROOT / "merchants" / "seeds"


class AttackRequest(BaseModel):
    merchant: str
    attack_id: str
    surface: str
    text: str
    fill: dict[str, str] = {}


class FulfillRequest(BaseModel):
    order_ref: str
    sku: str
    force_wrong_variant: bool = False  # planted faults for evaluation only


def load_storefronts(
    *,
    keys_dir: Path = KEYS_DIR,
    registry_path: Path = REGISTRY_PATH,
    seeds_dir: Path = SEEDS_DIR,
    profiles_dir: Path = CONFIG_DIR / "merchants",
) -> dict[str, Storefront]:
    records = load_records(registry_path)
    stores = {}
    for path in sorted(profiles_dir.glob("*.yaml")):
        profile: dict[str, Any] = yaml.safe_load(path.read_text())
        key = profile["key"]
        if key not in records:
            continue  # not onboarded yet
        stores[key] = Storefront(
            profile,
            latest_seed(seeds_dir, key),
            load_or_create(keys_dir, key),
            records[key].paypal_merchant_id,
        )
    return stores


def create_app(stores: dict[str, Storefront], *, allow_planted: bool = False) -> FastAPI:
    app = FastAPI(title="Provenant merchant simulator")

    def store(merchant: str) -> Storefront:
        try:
            return stores[merchant]
        except KeyError:
            raise HTTPException(404, f"unknown merchant {merchant!r}") from None

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "merchants": sorted(stores)}

    @app.get("/m/{merchant}/.well-known/provenant-manifest.json")
    def manifest(merchant: str) -> dict[str, Any]:
        return store(merchant).envelope.to_dict()

    @app.get("/m/{merchant}/.well-known/provenant-keys.json")
    def keys(merchant: str) -> dict[str, str]:
        return store(merchant).public_key()

    @app.get("/m/{merchant}/products/{sku}", response_class=HTMLResponse)
    def product_page(merchant: str, sku: str) -> str:
        try:
            return store(merchant).product_page(sku)
        except StorefrontError as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/m/{merchant}/catalog", response_class=HTMLResponse)
    def catalog_page(merchant: str) -> str:
        return store(merchant).catalog_page()

    def planted_only() -> None:
        if not allow_planted:
            raise HTTPException(403, "planted attacks are disabled (SIMULATOR_ALLOW_PLANTED)")

    @app.get("/admin/attack")
    def current_attack() -> dict[str, Any]:
        planted_only()
        active = [
            {"merchant": k, "attack_id": s.planted.attack_id, "surface": s.planted.surface}
            for k, s in stores.items()
            if s.planted is not None
        ]
        return {"active": active}

    @app.post("/admin/attack")
    def plant_attack(body: AttackRequest) -> dict[str, Any]:
        """Place exactly one attack: any previously planted attack is removed first."""
        planted_only()
        target = store(body.merchant)
        for s in stores.values():
            s.plant(None)
            s.lab_mode = True
        try:
            target.plant(PlantedAttack(body.attack_id, body.surface, body.text, dict(body.fill)))
            # Render once now so a payload with a missing value fails here, not mid-evaluation.
            target._payloads(target.seed.products[0].sku, body.surface)  # noqa: SLF001
        except (StorefrontError, KeyError, ValueError, IndexError) as e:
            target.plant(None)
            raise HTTPException(422, f"cannot plant: {e}") from None
        return current_attack()

    @app.delete("/admin/attack")
    def clear_attack() -> dict[str, Any]:
        planted_only()
        for s in stores.values():
            s.plant(None)
            s.lab_mode = False
        return {"active": []}

    @app.get("/m/{merchant}/reviews/{sku}", response_class=HTMLResponse)
    def reviews_page(merchant: str, sku: str) -> str:
        try:
            return store(merchant).reviews_page(sku)
        except StorefrontError as e:
            raise HTTPException(404, str(e)) from None

    @app.post("/m/{merchant}/fulfill")
    def fulfill(merchant: str, body: FulfillRequest) -> dict[str, Any]:
        if body.force_wrong_variant and not allow_planted:
            raise HTTPException(403, "planted faults are disabled (SIMULATOR_ALLOW_PLANTED)")
        try:
            s = store(merchant).fulfill(
                body.order_ref, body.sku, force_wrong_variant=body.force_wrong_variant
            )
        except StorefrontError as e:
            raise HTTPException(404, str(e)) from None
        return {
            "order_ref": s.order_ref,
            "ordered_sku": s.ordered_sku,
            "shipped_sku": s.shipped_sku,
            "shipped_attributes": s.shipped_attributes,
            "tracking_number": s.tracking_number,
        }

    return app


def _app() -> FastAPI:  # pragma: no cover  (wired at server start)
    return create_app(
        load_storefronts(), allow_planted=os.environ.get("SIMULATOR_ALLOW_PLANTED") == "1"
    )


def __getattr__(name: str) -> FastAPI:  # pragma: no cover
    # `uvicorn merchants.app:app` builds the app lazily, so importing this module in tests does
    # not require seeds, keys or a registry on disk.
    if name == "app":
        return _app()
    raise AttributeError(name)
