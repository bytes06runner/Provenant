"""Run the baseline agent once, optionally with one Adversarial Lab attack planted.

  python scripts/baseline_purchase.py --request "Buy me black trail running shoes, US size 10, ..."
        [--attack payee_swap.direct_instruction.0 --host northwind --surface product_page]

Creates a real sandbox PayPal order through PayPal's Agent Toolkit (never approved here) and
records every step in the Flight Recorder under a "b-..." session. Needs the merchant simulator
running with SIMULATOR_ALLOW_PLANTED=1 when an attack is planted.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from baseline.agent import BaselineDeps, run_baseline, toolkit_create_order  # noqa: E402
from lab.generate import latest_dataset  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402
from paypal.config import load_yaml  # noqa: E402


def plant(rt: Runtime, attack_id: str, host: str, surface: str) -> dict[str, Any]:
    data = latest_dataset()
    if data is None:
        raise SystemExit("no attack dataset: run python -m lab.generate")
    attack = next((a for a in data["attacks"] if a["id"] == attack_id), None)
    if attack is None:
        raise SystemExit(f"unknown attack {attack_id}")
    tax = load_yaml("attacks/taxonomy.yaml")
    values = tax["placement"]["attacker_values"]
    atk = rt.records["attacker"]
    fill = {
        "attacker_shop": "the authorized checkout partner (shop id: attacker)",
        "payee_name": atk.paypal_merchant_id,
        "address": values["address"],
        "quantity": values["quantity"],
    }
    base = rt.records[host].base_url.rsplit("/m/", 1)[0]
    r = rt.http.post(
        f"{base}/admin/attack",
        json={
            "merchant": host,
            "attack_id": attack_id,
            "surface": surface,
            "text": attack["text"],
            "fill": fill,
        },
    )
    r.raise_for_status()
    return dict(attack)


def clear(rt: Runtime) -> None:
    base = next(iter(rt.records.values())).base_url.rsplit("/m/", 1)[0]
    rt.http.delete(f"{base}/admin/attack")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--request", required=True)
    ap.add_argument("--user", default="buyer_a")
    ap.add_argument("--attack")
    ap.add_argument("--host", default="northwind")
    ap.add_argument("--surface", default="product_page")
    args = ap.parse_args()
    rt = Runtime.load()
    cfg = load_yaml("baseline.yaml")
    session = f"b-{uuid.uuid4().hex[:12]}"

    def record(event_type: str, payload: dict[str, Any]) -> None:
        rt.recorder.append(session, event_type, payload)

    rt.record_into(record)
    clear(rt)
    if args.attack:
        attack = plant(rt, args.attack, args.host, args.surface)
        record(
            "lab.attack",
            {"id": args.attack, "host": args.host, "surface": args.surface, "goal": attack["goal"]},
        )
    try:
        deps = BaselineDeps(
            router=rt.router,
            merchants={k: r.base_url for k, r in rt.records.items()},
            http=rt.http,
            create_order=toolkit_create_order(rt.credentials, str(cfg["toolkit_source"])),
            record=record,
            settings=cfg,
            read_order=lambda m, oid: rt.paypal(m).get(f"/v2/checkout/orders/{oid}").body,
        )
        address = dict(next(iter(rt.users[args.user]["addresses"].values())))
        run = run_baseline(deps, args.request, address)
    finally:
        clear(rt)
    out: dict[str, Any] = {"session": session, "steps": [(s["tool"], s["args"]) for s in run.steps]}
    if run.order:
        o = run.order["order"]
        out["order"] = {
            "merchant": run.order["merchant"],
            "id": o.get("id"),
            **run.order.get("paypal", {}),
        }
    out["finished"], out["error"] = run.finished, run.error
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
