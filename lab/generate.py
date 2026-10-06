"""Adversarial Lab: generate the prompt-injection dataset from config/attacks/taxonomy.yaml.

    python -m lab.generate [--dry-run]

For every (goal, technique) cell, the seed_generator model writes `variants_per_cell` payloads
with placeholders; each is validated (required placeholders present, no unknown ones) and the
dataset is frozen to lab/datasets/attacks-<YYYYMMDD>.json with a content hash. The simulator
places one payload at a time on one surface when an evaluation activates it, so every run of the
eval sees the same attacks. Generation runs once, at build time; evaluations never call it.
"""

from __future__ import annotations

import argparse
import json
import re
import string
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lineage.canonical import content_hash
from llm import roles
from llm.types import AllTargetsExhausted, LLMError, LLMRequest, Message
from paypal.config import load_yaml

DATASETS = Path(__file__).resolve().parent / "datasets"
PLACEHOLDERS = ("payee_name", "attacker_shop", "price", "quantity", "address", "sku", "other_sku")
_FIELD = re.compile(r"\{([^{}]*)\}")


def placeholders_in(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f is not None and f != ""}


def check_payload(text: str, must: list[str]) -> str | None:
    """None if the payload is usable, else why not."""
    try:
        found = placeholders_in(text)
    except ValueError as e:
        return f"unbalanced braces: {e}"
    unknown = found - set(PLACEHOLDERS)
    if unknown:
        return f"unknown placeholders {sorted(unknown)}"
    missing = {m.strip("{}") for m in must} - found
    if missing:
        return f"missing placeholders {sorted(missing)}"
    return None


def prompt_for(tax: dict[str, Any], goal: str, technique: str) -> str:
    g, n = tax["goals"][goal], int(tax["variants_per_cell"])
    text = roles.prompt("attack_generator_v1")
    fill = {
        "n": str(n),
        "goal": goal,
        "goal_description": g["description"],
        "technique": technique,
        "technique_description": tax["techniques"][technique],
        "must_mention": ", ".join(g["must_mention"]),
        "placeholders": ", ".join("{" + p + "}" for p in PLACEHOLDERS),
    }
    # Only our own markers are substituted; the braces meant for the model stay as written.
    for k, v in fill.items():
        text = text.replace("{" + k + "}", v)
    return text


def generate(
    router: roles.Router,
    tax: dict[str, Any],
    *,
    tries: int = 3,
    existing: list[dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_waits: int = 8,
) -> list[dict[str, Any]]:
    """Payloads for every cell. Cells already complete in `existing` are kept as they are, so
    a rerun only fills the gaps. A busy model is waited out; it does not use up an attempt."""
    out: list[dict[str, Any]] = []
    have: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in existing or []:
        have.setdefault((a["goal"], a["technique"]), []).append(a)
    n = int(tax["variants_per_cell"])
    for goal, g in tax["goals"].items():
        for technique in tax["techniques"]:
            old = have.get((goal, technique), [])
            if len(old) >= n:
                out.extend(old[:n])
                continue
            kept: list[str] = [a["text"] for a in old]
            model = old[0]["model"] if old else None
            attempt, waits = 0, 0
            while attempt < tries and len(kept) < n:
                req = roles._request(  # noqa: SLF001
                    router,
                    "seed_generator",
                    prompt_for(tax, goal, technique),
                    "Write them now.",
                    "attack_generator_v1",
                    seed=int(tax["seed"]) + attempt,
                )
                attempt += 1
                try:
                    resp = router.call(
                        "seed_generator",
                        LLMRequest(
                            messages=(req.messages[0], Message("user", req.messages[1].content)),
                            temperature=req.temperature,
                            max_tokens=req.max_tokens,
                            seed=req.seed,
                            json_schema=req.json_schema,
                            schema_name=req.schema_name,
                            structured=req.structured,
                        ),
                    )
                except AllTargetsExhausted:
                    if waits < max_waits:
                        waits += 1
                        attempt -= 1  # a busy model is not a failed attempt
                        sleep(20)
                    continue
                except LLMError:
                    continue  # one bad reply costs one attempt, not the run
                model = f"{resp.provider}/{resp.model}"
                try:
                    payloads = roles.parse_json(resp.text).get("payloads", [])
                except (ValueError, AttributeError):
                    continue
                for p in payloads:
                    if (
                        isinstance(p, str)
                        and not check_payload(p, g["must_mention"])
                        and p not in kept
                    ):
                        kept.append(p.strip())
            for i, text in enumerate(kept[:n]):
                out.append(
                    {
                        "id": f"{goal}.{technique}.{i}",
                        "goal": goal,
                        "technique": technique,
                        "text": text,
                        "model": model,
                    }
                )
    return out


def freeze(attacks: list[dict[str, Any]], tax: dict[str, Any], today: str) -> Path:
    DATASETS.mkdir(exist_ok=True)
    body = {
        "version": tax["version"],
        "seed": tax["seed"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "taxonomy_hash": content_hash(tax),
        "prompt": "attack_generator_v1",
        "attacks": attacks,
    }
    body["dataset_hash"] = content_hash(
        {"attacks": attacks, "taxonomy_hash": body["taxonomy_hash"]}
    )
    path = DATASETS / f"attacks-{today}.json"
    path.write_text(json.dumps(body, indent=2) + "\n")
    return path


def latest_dataset() -> dict[str, Any] | None:
    files = sorted(DATASETS.glob("attacks-*.json"))
    return json.loads(files[-1].read_text()) if files else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the first prompt and exit")
    args = ap.parse_args()
    tax = load_yaml("attacks/taxonomy.yaml")
    if args.dry_run:
        goal, technique = next(iter(tax["goals"])), next(iter(tax["techniques"]))
        print(prompt_for(tax, goal, technique))
        return 0
    from lineage.runtime import Runtime

    rt = Runtime.load()
    prev = latest_dataset()
    keep = prev["attacks"] if prev and prev.get("taxonomy_hash") == content_hash(tax) else None
    attacks = generate(rt.router, tax, existing=keep)
    path = freeze(attacks, tax, datetime.now(UTC).strftime("%Y%m%d"))
    cells = len(tax["goals"]) * len(tax["techniques"])
    print(f"{len(attacks)} payloads for {cells} cells -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
