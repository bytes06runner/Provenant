"""End-to-end sandbox purchase: request -> mandate -> plan -> contracts -> PayPal order.

Needs the merchant simulator running (uvicorn merchants.app:app --port 8710), seeds, keys and
var/registry.json (scripts/seed_catalog.py, scripts/register_merchants.py), and .env.

  python scripts/demo_purchase.py --request "..." [--set max_total='"120.00"'] [--yes]
                                  [--review-sku NOR-001] [--attack payee-swap] [--wait-approval]

The mandate is shown first. If the extractor raised questions or the proposal has problems, the
script stops: answer by setting fields with --set name=<json> and confirm with --yes. Nothing
ambiguous is resolved silently.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lineage.probes import payee_swap  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402
from llm import roles  # noqa: E402


def say(title: str, data: Any = None) -> None:
    print(f"\n== {title}")
    if data is not None:
        print(json.dumps(data, indent=2, default=str) if not isinstance(data, str) else data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--user", default="buyer_a")
    ap.add_argument("--request", required=True, help="what the shopper asks for, in their words")
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="FIELD=JSON",
        help="the shopper's edit to a mandate field, e.g. max_total='\"120.00\"'",
    )
    ap.add_argument("--yes", action="store_true", help="the shopper confirms the mandate")
    ap.add_argument("--review-sku", help="give the planner this sku's reviews page")
    ap.add_argument("--review-merchant", default="northwind")
    ap.add_argument("--attack", choices=["payee-swap"], help="also try a live hijack")
    ap.add_argument(
        "--wait-approval", action="store_true", help="wait for PayPal approval, then authorize"
    )
    args = ap.parse_args()

    rt = Runtime.load()
    session = rt.new_session(args.user)
    session_id = session.session_id
    toolbox = session.toolbox
    records = rt.records
    recorder = rt.recorder
    spec = rt.spec
    user = rt.users[args.user]
    say(f"Session {session_id}")

    # 1. mandate
    proposal = session.propose(args.request, list(user["addresses"]))
    fields = dict(proposal.fields)
    for edit in args.set:
        name, _, value = edit.partition("=")
        fields[name] = json.loads(value)
    problems = roles._check_proposal(  # noqa: SLF001  (same checks after the user's edits)
        fields, spec["attributes"], list(user["addresses"]), toolbox.list_merchants()
    )
    say(
        "Proposed mandate (review before signing)",
        {"fields": fields, "questions": proposal.questions, "problems": problems},
    )
    if problems or (proposal.questions and not args.yes) or not args.yes:
        say("Stopped: the shopper must answer the questions (--set) and confirm (--yes).")
        return 3
    vm = session.confirm_and_sign(fields)
    say(
        "Mandate signed",
        {"mandate_hash": vm.envelope.payload_hash, "expires": vm.mandate.expires_at},
    )

    # 2. plan and run
    review_url = None
    if args.review_sku:
        review_url = f"{records[args.review_merchant].base_url}/reviews/{args.review_sku}"
    plan = session.plan(vm, review_url)
    say("Plan", {"plan_hash": plan.plan_hash, "statements": len(plan.body)})
    p = session.run(vm, plan)
    c = p.candidate
    say(
        "Proposal",
        {
            "merchant": c.manifest.merchant_id,
            **c.summary(),
            "decision_label": p.decision_label.name,
            "final_contract_check": "PASS" if p.result.allowed else "BLOCKED",
        },
    )

    # 3. live attacks
    if args.attack == "payee-swap":
        for name, r, info in payee_swap(
            session,
            toolbox,
            vm,
            p,
            attacker_id="attacker",
            attacker_base_url=records["attacker"].base_url,
        ):
            say(f"Hijack: {name}", {**info, **_violations(r)})

    # 4. PayPal order
    order = session.create_order(p)
    say("PayPal order created", dataclasses.asdict(order))
    if args.wait_approval:
        say("Waiting for buyer approval", order.approval_url)
        say("Authorized", session.authorize_when_approved(order))

    head = recorder.verify(session_id)
    bound = recorder.resolve_custom_id(session_id, order.custom_id, session.custom_id_format)
    say(
        "Flight Recorder",
        {
            "events": len(recorder.events(session_id)),
            "head": head,
            "custom_id_resolves_to_seq": bound.seq,
            "bound_event_type": bound.event_type,
        },
    )
    return 0


def _violations(result: Any) -> dict[str, Any]:
    return {
        "allowed": result.allowed,
        "violations": [
            {
                "field": v.field.value,
                "rule": v.rule.value,
                "detail": v.detail,
                "label": v.label.name if v.label is not None else None,
                "provenance": list(v.provenance),
            }
            for v in result.violations
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
