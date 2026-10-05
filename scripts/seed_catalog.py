"""Generate and freeze a catalog seed for each simulated merchant.

Attributes, prices and stock are drafted deterministically from config/catalog/<spec>.yaml and the
merchant's seed (merchants/catalog.py). An LLM (role `seed_generator`) writes only titles,
descriptions and reviews. The result is frozen to merchants/seeds/<merchant>-<YYYYMMDD>.json and
committed, so demos and evaluations replay the same catalog without calling a model again.

Run: python scripts/seed_catalog.py [--merchant northwind] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine  # noqa: E402

from lineage.canonical import content_hash  # noqa: E402
from llm import LLMRequest, Message, build_router  # noqa: E402
from merchants.catalog import Review, Seed, draft_products  # noqa: E402
from paypal.config import CONFIG_DIR, load_yaml  # noqa: E402

SEEDS_DIR = REPO_ROOT / "merchants" / "seeds"
PROMPT = REPO_ROOT / "llm" / "prompts" / "catalog_text_v1.md"
PROMPT_VERSION = "catalog_text_v1"

TEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["products"],
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sku", "title", "description", "reviews"],
                "properties": {
                    "sku": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "reviews": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["rating", "text"],
                            "properties": {
                                "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                                "text": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}

PERSONALITY = {
    "honest": "a careful outdoor specialist",
    "sloppy": "a busy discount warehouse",
    "misrepresenting": "a hype-driven brand that oversells",
    "attacker": "a flashy deals site",
}


def seed_for(key: str, router, *, today: str) -> Seed:
    profile = load_yaml(f"merchants/{key}.yaml")
    spec = load_yaml(f"catalog/{profile['catalog_spec']}.yaml")
    drafts = draft_products(spec, profile)
    system = (
        PROMPT.read_text()
        .replace("{reviews_per_product}", str(spec["reviews_per_product"]))
        .replace("{store_name}", profile["display_name"])
        .replace("{profile}", PERSONALITY[profile["profile"]])
    )
    # The copy describes what the merchant claims (signed attributes), as a real store would.
    items = [{"sku": p.sku, "attributes": p.attributes} for p in drafts]
    resp = router.call(
        "seed_generator",
        LLMRequest(
            messages=(Message("system", system), Message("user", json.dumps(items))),
            temperature="0.7",
            max_tokens=6000,
            json_schema=TEXT_SCHEMA,
            schema_name="catalog_text",
            structured="schema",
        ),
    )
    text = resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    written = {p["sku"]: p for p in json.loads(text)["products"]}
    missing = [p.sku for p in drafts if p.sku not in written]
    if missing:
        raise SystemExit(f"{key}: model skipped skus {missing}; rerun")
    for p in drafts:
        w = written[p.sku]
        p.title = w["title"].strip()[:127]
        p.description = w["description"].strip()
        p.reviews = [Review(int(r["rating"]), r["text"].strip()) for r in w["reviews"]]
    return Seed(
        merchant=key,
        category=spec["category"],
        currency=spec["currency"],
        created=today,
        generator={
            "prompt": PROMPT_VERSION,
            "model": f"{resp.provider}/{resp.model}",
            "model_version": resp.model_version,
            "response_sha256": content_hash({"text": resp.text}),
            "usage": resp.usage.to_dict(),
        },
        products=drafts,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--merchant", help="one merchant key (default: all)")
    ap.add_argument("--force", action="store_true", help="regenerate even if today's seed exists")
    args = ap.parse_args()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    stamp = today.replace("-", "")
    keys = (
        [args.merchant]
        if args.merchant
        else sorted(p.stem for p in (CONFIG_DIR / "merchants").glob("*.yaml"))
    )
    (REPO_ROOT / "var").mkdir(exist_ok=True)
    router = build_router(create_engine(f"sqlite:///{REPO_ROOT / 'var' / 'llm.db'}"))
    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    for key in keys:
        out = SEEDS_DIR / f"{key}-{stamp}.json"
        if out.exists() and not args.force:
            print(f"{key}: {out.name} exists, skipping (use --force)")
            continue
        seed = seed_for(key, router, today=today)
        out.write_text(seed.to_json())
        print(f"{key}: wrote {out.name} ({len(seed.products)} products, {seed.generator['model']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
