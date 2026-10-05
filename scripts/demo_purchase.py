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
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from blackbox.recorder import CustomIdFormat, FlightRecorder  # noqa: E402
from lineage.labels import untrusted  # noqa: E402
from lineage.nonces import SqlNonceRegistry  # noqa: E402
from lineage.purchase import PurchaseSession  # noqa: E402
from lineage.signing import key_id  # noqa: E402
from lineage.toolbox import HttpToolbox  # noqa: E402
from lineage.vault import Address, AddressVault  # noqa: E402
from llm import build_router, roles  # noqa: E402
from merchants.keystore import load_or_create  # noqa: E402
from merchants.registry import key_registry, load_records  # noqa: E402
from paypal.client import PayPalClient  # noqa: E402
from paypal.config import (  # noqa: E402
    http_settings,
    load_env,
    load_yaml,
    merchant_credentials,
    require_env,
)
from paypal.ledger import RequestLedger  # noqa: E402

VAR = REPO_ROOT / "var"


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

    load_env()
    app_cfg = load_yaml("app.yaml")
    settings = app_cfg["purchase"]
    users = load_yaml("demo/users.yaml")["users"]
    user = users[args.user]
    records = load_records(VAR / "registry.json")
    engine = create_engine(f"sqlite:///{VAR / 'provenant.db'}")
    recorder = FlightRecorder(engine)
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    holder: dict[str, PurchaseSession] = {}

    def record(event_type: str, payload: dict[str, Any]) -> None:
        holder["s"].record(event_type, payload)

    router = build_router(engine, record=record)
    ledger = RequestLedger.from_url(f"sqlite:///{VAR / 'ledger.db'}")
    http = httpx.Client(timeout=30)
    toolbox = HttpToolbox(
        merchants={k: r.base_url for k, r in records.items()},
        keys=key_registry(records),
        router=router,
        http=http,
        put_blob=recorder.put_blob,
        record=record,
        seed=int(settings["q_llm_seed"]),
    )
    vault = AddressVault()
    for ref, addr in user["addresses"].items():
        vault.save_confirmed(args.user, ref, Address(**addr))
    user_key = load_or_create(VAR / "keys" / "users", args.user)
    cid = app_cfg["paypal"]["custom_id"]
    spec = load_yaml("catalog/trail_running.yaml")
    session = PurchaseSession(
        session_id=session_id,
        recorder=recorder,
        router=router,
        toolbox=toolbox,
        vault=vault,
        user_id=args.user,
        user_key=user_key,
        user_keys={key_id(user_key.public_key()): user_key.public_key()},
        nonces=SqlNonceRegistry(engine),
        custom_id_format=CustomIdFormat(
            cid["prefix"], int(cid["hash_hex_chars"]), int(cid["max_length"])
        ),
        paypal=lambda mid: PayPalClient(merchant_credentials(mid), http_settings(), ledger=ledger),
        settings=settings,
        category_spec=spec,
        now=lambda: datetime.now(UTC),
        experience_context={
            "user_action": "CONTINUE",
            "return_url": require_env("PAYPAL_RETURN_URL"),
            "cancel_url": require_env("PAYPAL_CANCEL_URL"),
        },
    )
    holder["s"] = session
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
        atk = toolbox.fetch_manifest("attacker")
        url = f"{records['attacker'].base_url}/reviews/{atk.manifest.catalog[0].sku}"
        text, digest = toolbox.fetch_page(url)
        found = toolbox.extract("payment_instructions_v1", text) or {}
        injected = found.get("payee")
        say("Attacker page, as read by the Q-LLM", {"url": url, "extracted": found})
        if injected:
            hijacked = dataclasses.replace(
                c.checkout,
                payee=untrusted(
                    injected, f"page:{url}", "qllm:payment_instructions_v1.payee", digest
                ),
            )
            r = session.check_hijack(vm, p, hijacked, "payee from injected page text")
            say("Hijack 1: payee taken from the attacker's page", _violations(r))
        hijacked = dataclasses.replace(c.checkout, payee=atk.payee())
        r = session.check_hijack(vm, p, hijacked, "attacker's own signed payee")
        say("Hijack 2: attacker's validly signed payee swapped in", _violations(r))

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
