"""Phase 2 acceptance: three planted-fault scenarios, end to end on the sandbox.

  python scripts/planted_scenarios.py --create      # 3 purchases -> 3 PayPal orders to approve
  python scripts/planted_scenarios.py --authorize   # waits for buyer approvals, authorizes
  python scripts/planted_scenarios.py --resolve [--k 4]
        # fulfills where planted, files each complaint, runs the case WITH remedy approval,
        # and checks majority fault and money movement against the planted answer

Scenarios and expectations: config/eval/planted.yaml. State: var/planted.json.
The simulator must run with SIMULATOR_ALLOW_PLANTED=1 for the wrong-variant scenario.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from blackbox.case import run_case  # noqa: E402
from blackbox.intake import Complaint  # noqa: E402
from blackbox.replay import ReplaySettings  # noqa: E402
from lab.synthetic import sneaker_photo  # noqa: E402
from lineage.fulfillment import authorize_recorded, fulfill_and_capture  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402
from llm import roles  # noqa: E402
from paypal.config import load_yaml  # noqa: E402

STATE = REPO_ROOT / "var" / "planted.json"
USER = "buyer_a"


def load_state() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_entry(name: str, entry: dict[str, Any]) -> None:
    """Re-read before writing so two concurrent phases never drop each other's scenarios."""
    state = load_state()
    state[name] = entry
    STATE.write_text(json.dumps(state, indent=2))


def create(rt: Runtime, cfg: dict[str, Any]) -> None:
    state = load_state()
    for name, sc in cfg["scenarios"].items():
        if name in state:
            print(f"{name}: already created ({state[name]['session']})")
            continue
        s = rt.new_session(USER, rank_prompt=sc["rank_prompt"])
        budget = sc.get("budget", cfg["budget"])
        request = f"{sc['request']} {cfg['request_suffix'].format(budget=budget)}"
        proposal = s.propose(request, list(rt.users[USER]["addresses"]))
        fields = dict(proposal.fields)
        for k, v in cfg["answers"].items():
            if fields.get(k) is None:
                fields[k] = v.format(budget=budget)
        fields.update(sc.get("extra_answers", {}))
        problems = roles._check_proposal(  # noqa: SLF001
            fields,
            rt.spec["attributes"],
            list(rt.users[USER]["addresses"]),
            s.toolbox.list_merchants(),
        )
        if problems:
            raise SystemExit(f"{name}: mandate problems {problems}; fields {fields}")
        vm = s.confirm_and_sign(fields)
        review = sc.get("review_sku")
        url = (
            f"{rt.records[review['merchant']].base_url}/reviews/{review['sku']}" if review else None
        )
        p = s.run(vm, s.plan(vm, url))
        order = s.create_order(p)
        save_entry(
            name,
            {
                "session": s.session_id,
                "order_id": order.order_id,
                "merchant": order.merchant_id,
                "chosen": p.candidate.summary(),
                "approval_url": order.approval_url,
            },
        )
        print(
            f"{name}: {order.merchant_id} {p.candidate.summary()['sku']} "
            f"total {p.candidate.summary()['total']}\n  approve: {order.approval_url}"
        )


def authorize(rt: Runtime, only: set[str] | None) -> None:
    state = load_state()
    for name, st in state.items():
        if only and name not in only:
            continue
        a = authorize_recorded(
            recorder=rt.recorder,
            session_id=st["session"],
            paypal=rt.paypal,
            poll_seconds=5,
            timeout_seconds=3600,
            sleep=time.sleep,
            clock=time.monotonic,
        )
        st["authorization"] = a
        save_entry(name, st)
        print(f"{name}: authorized {a['authorization_id']} {a['status']}")


def resolve(rt: Runtime, cfg: dict[str, Any], k: int, only: set[str] | None) -> int:
    state = load_state()
    rows = []
    for name, sc in cfg["scenarios"].items():
        if only and name not in only:
            continue
        st = state[name]
        session = st["session"]
        if sc["fulfill"] and "fulfillment" not in st:
            st["fulfillment"] = fulfill_and_capture(
                recorder=rt.recorder,
                session_id=session,
                storefront_url=rt.records[st["merchant"]].base_url,
                http=rt.http,
                paypal=rt.paypal,
                force_wrong_variant=bool(sc.get("planted_wrong_variant")),
            )
            save_entry(name, st)
        photo = None
        if sc["photo"]:
            color = st["fulfillment"]["shipment"]["shipped_attributes"]["color"]
            photo = sneaker_photo(color)
        clarify: dict[str, Any] = {}
        signed = next(e for e in rt.recorder.events(session) if e.event_type == "mandate.signed")
        base = signed.payload["envelope"]["payload"]
        for item in sc["clarify"]:
            key, _, value = item.partition("=")
            section, attr = key.split(".", 1)
            if section == "required":
                req = dict(clarify.get("required_attributes", base["required_attributes"]))
                req[attr] = value
                clarify["required_attributes"] = req
        complaint = Complaint(
            text=sc["complaint"],
            clarified=clarify,
            reported_attributes=dict(x.split("=", 1) for x in sc["report"]),
            photo_png=photo,
        )
        r = run_case(rt.case_deps(USER), session, complaint, k=k, approve=True)
        majority = None
        if r.attribution and r.attribution.get("majority"):
            majority = r.attribution["majority"]
        kinds = sorted({x["kind"] for x in r.executed})
        ok = majority == sc["expect"]["majority"] and sc["expect"]["money"] in kinds
        rows.append(
            {
                "scenario": name,
                "planted": sc["planted"],
                "expected": sc["expect"],
                "majority": majority,
                "shares": (r.attribution or {}).get("shares"),
                "money": r.executed,
                "settled": r.settled,
                "pass": ok,
                "case": r.case_id,
                "evidence_pack": str(r.evidence_pdf),
            }
        )
        st["result"] = rows[-1]
        save_entry(name, st)
        print(json.dumps(rows[-1], indent=2, default=str))
    print("\nSUMMARY")
    for row in rows:
        ids = ", ".join(f"{m['kind']} {m['resource_id']} {m['status']}" for m in row["money"])
        print(
            f"  {row['scenario']:<14} expected {row['expected']['majority']}/"
            f"{row['expected']['money']:<7} got {row['majority']}  {ids}  "
            f"{'PASS' if row['pass'] else 'FAIL'}"
        )
    return 0 if all(r["pass"] for r in rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--create", action="store_true")
    g.add_argument("--authorize", action="store_true")
    g.add_argument("--resolve", action="store_true")
    ap.add_argument("--k", type=int)
    ap.add_argument("--only", nargs="+", help="scenario names (default: all)")
    args = ap.parse_args()
    rt = Runtime.load()
    cfg = load_yaml("eval/planted.yaml")
    if args.create:
        create(rt, cfg)
    elif args.authorize:
        authorize(rt, set(args.only or []))
    else:
        return resolve(rt, cfg, ReplaySettings.for_run(args.k).k, set(args.only or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
