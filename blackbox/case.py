"""One recourse case, end to end (CLAUDE.md section 4.7, steps A to D).

    load the purchase from its recorder session (verified chain)
    file the complaint (its own hash-chained case session)
    sign the clarified intent; check the photo (vision, escalating only when needed)
    establish facts; read the order's live state from PayPal
    fulfillment fault -> merchant owns 100% (no replay needed)
    decision wrong    -> replay every coalition k times, exact Shapley with bootstrap CIs
    remedy plan (deterministic), ruling text (numbers only), evidence pack PDF
    with approval only: execute on PayPal, reconcile until final, report discrepancies

Replays run "as of" the purchase: the original mandate and the clarified intent are evaluated
inside the original validity window, against the merchant content recorded at the time.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from blackbox.attribution import PLAYERS, Attribution, attribute, label
from blackbox.facts import Facts, establish
from blackbox.intake import Complaint, PurchaseRecord, file_complaint, load_purchase
from blackbox.narrate import narrate
from blackbox.reconcile import ReconciliationPoller
from blackbox.recorder import FlightRecorder
from blackbox.remedy import RemedyPlan, plan_remedy
from blackbox.remedy.evidence import build_evidence_pack
from blackbox.remedy.execute import execute
from blackbox.replay import (
    Content,
    Policy,
    Truth,
    WaitPolicy,
    World,
    best_total,
    correct_manifest,
    judge,
    pages_from_blobs,
    run_all,
)
from lineage.mandate import IntentMandate, VerifiedMandate, sign_mandate, verify_mandate
from lineage.manifest import (
    ManifestError,
    MerchantKeyRegistry,
    VerifiedManifest,
    verify_manifest,
)
from lineage.money import parse_amount
from lineage.vault import AddressVault
from llm import roles
from llm.types import Image
from paypal.client import PayPalClient


class CaseError(RuntimeError):
    pass


@dataclass
class CaseDeps:
    recorder: FlightRecorder
    router: roles.Router
    record_into: Callable[[Callable[[str, dict[str, Any]], None]], None]
    user_id: str
    user_key: Ed25519PrivateKey
    user_keys: dict[str, Ed25519PublicKey]
    vault: AddressVault
    keys: MerchantKeyRegistry
    fetch_manifest: Callable[[str], VerifiedManifest]  # live, for sessions recorded before blobs
    paypal: Callable[[str], PayPalClient]  # merchant id or "operator"
    poller: ReconciliationPoller
    caused: Callable[[], set[str]]  # resource ids our ledger created
    buyer_email: str
    allowed_colors: list[str]
    attribution_cfg: dict[str, Any]
    q_llm_seed: int | None
    out_dir: Path
    sleep: Callable[[float], None]
    max_settle_rounds: int = 60
    wait_attempts: int = 12  # waiting for a busy reference model: attempts x seconds
    wait_seconds: float = 15.0
    agent_rank_default: str = "rank_v1"


@dataclass
class CaseResult:
    case_id: str
    facts: Facts
    attribution: dict[str, Any] | None
    plan: RemedyPlan
    ruling: dict[str, Any]
    evidence_pdf: Path
    executed: list[dict[str, Any]] = field(default_factory=list)
    settled: bool | None = None
    discrepancies: list[dict[str, Any]] = field(default_factory=list)


# ---- helpers -------------------------------------------------------------------------


def _manifests(deps: CaseDeps, purchase: PurchaseRecord) -> dict[str, VerifiedManifest]:
    out = {}
    for mid, recorded_hash in purchase.manifest_hashes.items():
        env = purchase.manifest_envelopes.get(mid)
        vm = (
            verify_manifest(env, merchant_id=mid, registry=deps.keys)
            if env
            else deps.fetch_manifest(mid)
        )
        if vm.hash != recorded_hash:
            raise CaseError(
                f"{mid}: manifest hash {vm.hash[:12]} differs from the one recorded "
                f"at purchase time {recorded_hash[:12]}"
            )
        out[mid] = vm
    return out


def _sign_clarified(
    deps: CaseDeps, original: VerifiedMandate, edits: dict[str, Any]
) -> VerifiedMandate:
    doc = original.mandate.model_dump()
    doc.update(edits)
    doc["nonce"] = secrets.token_hex(16)
    doc["mandate_id"] = f"{original.mandate.mandate_id}-clarified"
    clarified = IntentMandate.model_validate(doc)
    env = sign_mandate(clarified, deps.user_key)
    return verify_mandate(
        env,
        user_id=deps.user_id,
        user_keys=deps.user_keys,
        now=original.mandate.issued_at + timedelta(seconds=1),
    )


def _paypal_state(deps: CaseDeps, purchase: PurchaseRecord) -> dict[str, Any]:
    with deps.paypal(purchase.merchant_id) as c:
        order = c.get(f"/v2/checkout/orders/{purchase.order_id}").body
    pay = (order.get("purchase_units") or [{}])[0].get("payments") or {}
    auths = pay.get("authorizations", [])
    caps = [
        x
        for x in pay.get("captures", [])
        if x.get("status") in ("COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED")
    ]
    refunded = sum(
        (
            parse_amount(r["amount"]["value"])
            for r in pay.get("refunds", [])
            if r.get("status") == "COMPLETED"
        ),
        Decimal("0"),
    )
    return {
        "order_status": order.get("status"),
        "authorization_id": auths[0]["id"] if auths else None,
        "authorization_status": auths[0].get("status") if auths else None,
        "capture_id": caps[0]["id"] if caps else None,
        "captured": caps[0]["amount"]["value"] if caps else None,
        "currency": (caps[0]["amount"]["currency_code"] if caps else "USD"),
        "refunded": str(refunded),
    }


def _fault_from_facts() -> Attribution:
    zero = (Fraction(0), Fraction(0))
    one = (Fraction(1), Fraction(1))
    return Attribution(
        k=0,
        v={},
        phi={"U": Fraction(0), "M": Fraction(1), "A": Fraction(0)},
        shares={"U": Fraction(0), "M": Fraction(1), "A": Fraction(0)},
        ci={"U": zero, "M": one, "A": zero},
        ci_level=Fraction(95, 100),
        escalate=False,
        reason="fulfillment fault established by the facts: the merchant owns 100 percent",
    )


def _percent(x: Fraction) -> str:
    return f"{float(x * 100):.1f}"


# ---- the case ----------------------------------------------------------------------------


def run_case(
    deps: CaseDeps, session_id: str, complaint: Complaint, *, k: int, approve: bool
) -> CaseResult:
    rec = deps.recorder
    purchase = load_purchase(rec, session_id)
    case = file_complaint(rec, purchase, complaint)

    def record(event_type: str, payload: dict[str, Any]) -> None:
        rec.append(case, event_type, payload)

    deps.record_into(record)  # LLM calls of this case land in the case's own chain

    original = verify_mandate(
        purchase.mandate_envelope,
        user_id=deps.user_id,
        user_keys=deps.user_keys,
        now=_purchase_time(purchase),
    )
    clarified = _sign_clarified(deps, original, complaint.clarified)
    record("mandate.clarified", {"envelope": clarified.envelope.to_dict()})

    delivery = None
    if complaint.photo_png is not None and purchase.shipment is not None:
        delivery = roles.check_delivery(
            deps.router,
            Image(complaint.photo_png, "image/png"),
            allowed_colors=deps.allowed_colors,
            record_color=purchase.shipment["shipped_attributes"].get("color"),
        )
    facts = establish(
        purchase,
        clarified_required=clarified.mandate.required_attributes,
        clarified_forbidden=clarified.mandate.forbidden_attributes,
        reported_attributes=complaint.reported_attributes,
        delivery=delivery,
    )
    record("facts.established", facts.to_dict())
    state = _paypal_state(deps, purchase)
    record("paypal.state", state)

    manifests = _manifests(deps, purchase)
    merchant = manifests[purchase.merchant_id]
    attribution: Attribution | None = None
    overpayment: Decimal | None = None
    note = ""
    if facts.fulfillment_fault:
        attribution = _fault_from_facts()
    else:
        corrections = (
            {purchase.sku: {a: d["actual"] for a, d in facts.misrepresentations.items()}}
            if facts.misrepresentations
            else {}
        )
        corrected = dict(manifests)
        if corrections:
            corrected[purchase.merchant_id] = correct_manifest(
                merchant, corrections, Ed25519PrivateKey.generate(), MerchantKeyRegistry()
            )
        established = (
            {(purchase.merchant_id, purchase.sku): facts.actual_attributes} if facts.shipped else {}
        )
        truth = Truth(corrected, established)
        chosen = {"merchant_id": purchase.merchant_id, "sku": purchase.sku, "total": purchase.total}
        observed_bad, why = judge(chosen, truth, clarified)
        best = best_total(truth, clarified)
        if (
            not facts.decision_wrong
            and best is not None
            and parse_amount(purchase.total) > best
            and clarified.mandate.preference is not None
        ):
            overpayment = parse_amount(purchase.total) - best
        record(
            "outcome.observed",
            {
                "bad": observed_bad,
                "why": why,
                "best_total": str(best) if best is not None else None,
            },
        )
        if observed_bad:
            observed_pages, corrected_pages = pages_from_blobs(purchase.pages, rec.get_blob)
            policy_event = next(
                (e for e in purchase.events if e.event_type == "agent.policy"), None
            )
            agent = Policy(
                "agent",
                "planner",
                policy_event.payload["planner_prompt"] if policy_event else "planner_v2",
                policy_event.payload["rank_prompt"] if policy_event else deps.agent_rank_default,
            )
            world = World(
                original=original,
                clarified=clarified,
                observed=Content(manifests, observed_pages),
                corrected=Content(corrected, corrected_pages),
                agent=agent,
                reference=Policy("reference", "reference_policy", "planner_reference_v1", None),
                vault=deps.vault,
                now=_purchase_time(purchase),
                truth=truth,
                q_llm_seed=deps.q_llm_seed,
            )
            cfg = deps.attribution_cfg
            crn = bool(cfg.get("common_random_numbers", False))
            samples = run_all(
                world,
                k,
                deps.router,
                record,
                WaitPolicy(deps.wait_attempts, deps.wait_seconds, deps.sleep),
                common_random_numbers=crn,
            )
            attribution = attribute(
                samples,
                resamples=int(cfg["bootstrap_resamples"]),
                ci_level=Fraction(str(cfg["ci_level"])),
                max_ci_width=Fraction(str(cfg["max_ci_width_for_auto"])),
                seed=case,
                paired=crn,
            )
        else:
            note = f"the purchase satisfies the clarified intent ({why}); no replay needed"
    att = attribution.to_dict() if attribution else None
    record("attribution", att or {"note": note})

    captured = parse_amount(state["captured"]) if state["captured"] else None
    plan = plan_remedy(
        facts=facts,
        attribution=attribution,
        captured=captured,
        already_refunded=parse_amount(state["refunded"]),
        caps=merchant.manifest.policy.refund_caps,
        overpayment=overpayment,
    )
    record("remedy.proposed", plan.to_dict())

    names = {"U": "user", "M": "merchant", "A": "agent"}
    summary = {
        "case_id": case,
        "order_id": purchase.order_id,
        "fact_lines": _fact_lines(facts),
        "shares_percent": (
            {names[p]: _percent(attribution.shares[p]) for p in PLAYERS}
            if attribution and attribution.shares
            else None
        ),
        "counterfactual": att["v"] if att else None,
        "intervals": att["ci"] if att else None,
        "remedy": plan.to_dict(),
        "holding": _holding(facts, attribution, plan),
        "remedy_line": _remedy_line(plan),
    }
    ruling = narrate(deps.router, summary)
    record("ruling", ruling)

    pack = _pack(deps, case, purchase, original, clarified, merchant, facts, att, plan, ruling)
    deps.out_dir.mkdir(parents=True, exist_ok=True)
    pdf = deps.out_dir / f"{case}.pdf"
    pdf.write_bytes(pack)
    record("evidence_pack", {"blob": rec.put_blob(pack, "application/pdf"), "bytes": len(pack)})
    result = CaseResult(case, facts, att, plan, ruling, pdf)

    if approve and plan.status == "proposed":
        record("remedy.approved", {"by": "operator", "plan": plan.to_dict()})
        result.executed = execute(
            plan,
            case_id=case,
            merchant_id=purchase.merchant_id,
            authorization_id=state["authorization_id"],
            capture_id=state["capture_id"],
            currency=state["currency"],
            buyer_email=deps.buyer_email,
            paypal=deps.paypal,
            poller=deps.poller,
            record=record,
        )
        result.settled = deps.poller.settle(
            case, max_rounds=deps.max_settle_rounds, sleep=deps.sleep
        )
        result.discrepancies = deps.poller.discrepancies(
            case_id=case, order_id=purchase.order_id, app=purchase.merchant_id, caused=deps.caused()
        )
    record(
        "case.closed",
        {
            "plan_status": plan.status,
            "executed": result.executed,
            "settled": result.settled,
            "discrepancies": len(result.discrepancies),
        },
    )
    return result


def _purchase_time(purchase: PurchaseRecord) -> Any:
    signed = next(e for e in purchase.events if e.event_type == "mandate.signed")
    return signed.recorded_at


def _fact_lines(f: Facts) -> list[str]:
    lines = [
        f"ordered {f.ordered_sku}"
        + (f", shipped {f.shipped_sku}" if f.shipped else ", not shipped yet")
    ]
    if f.fulfillment_fault:
        lines.append("the merchant shipped a different item than was ordered")
    for a, d in f.misrepresentations.items():
        lines.append(f"the merchant signed {a}={d['signed']} but the item has {a}={d['actual']}")
    lines += [f"against the clarified intent: {v}" for v in f.clarified_violations]
    lines += [f"conflict: {c}" for c in f.conflicts]
    return lines


def _holding(f: Facts, a: Attribution | None, plan: RemedyPlan) -> str:
    if plan.status == "needs_human_review":
        return "The evidence does not support an automatic ruling; the case goes to a human."
    if a is None or a.shares is None:
        return "No fault is found: the purchase satisfies the clarified intent."
    who = {"U": "the user's original wording", "M": "the merchant", "A": "the agent"}
    top = a.leaders()
    if len(top) > 1:
        return "Fault is shared equally by " + " and ".join(who[p] for p in top) + "."
    return f"The largest share of fault lies with {who[top[0]] if top else 'no one'}."


def _remedy_line(plan: RemedyPlan) -> str:
    if not plan.actions:
        return "No money moves."
    parts = [f"{x.kind} of {x.amount}" if x.amount else x.kind for x in plan.actions]
    return "Ordered: " + "; ".join(parts) + f". Absorbed by the user: {plan.absorbed_by_user}."


def _pack(
    deps: CaseDeps,
    case: str,
    purchase: PurchaseRecord,
    original: VerifiedMandate,
    clarified: VerifiedMandate,
    merchant: VerifiedManifest,
    facts: Facts,
    att: dict[str, Any] | None,
    plan: RemedyPlan,
    ruling: dict[str, Any],
) -> bytes:
    try:
        verify_manifest(merchant.envelope, merchant_id=merchant.merchant_id, registry=deps.keys)
        sig = "valid, verified against the key registered at onboarding"
    except ManifestError as e:
        sig = f"INVALID: {e}"
    m = original.mandate
    return build_evidence_pack(
        {
            "case_id": case,
            "order_id": purchase.order_id,
            "merchant_id": purchase.merchant_id,
            "custom_id": purchase.custom_id,
            "purchase_session": purchase.session_id,
            "purchase_chain_head": deps.recorder.verify(purchase.session_id),
            "mandate": {
                "hash": original.envelope.payload_hash,
                "key_id": original.envelope.key_id,
                "requirements": {
                    "required": m.required_attributes,
                    "forbidden": m.forbidden_attributes,
                    "max_total": m.max_total,
                    "preference": m.preference,
                },
                "clarified": {
                    "required": clarified.mandate.required_attributes,
                    "forbidden": clarified.mandate.forbidden_attributes,
                    "preference": clarified.mandate.preference,
                    "hash": clarified.envelope.payload_hash,
                },
            },
            "merchant": {
                "manifest_hash": merchant.hash,
                "key_id": merchant.envelope.key_id,
                "signature_check": sig,
                "sku": purchase.sku,
                "signed_attributes": purchase.signed_attributes,
            },
            "facts": {k: v for k, v in facts.to_dict().items() if k != "delivery_check"},
            "attribution": att if att and att.get("v") else None,
            "attribution_note": "Fault established by facts; no replay was needed."
            if att
            else "No replay: the purchase satisfies the clarified intent.",
            "remedy": plan.to_dict(),
            "ruling": ruling["ruling"],
        }
    )


__all__ = ["CaseDeps", "CaseResult", "label", "run_case"]
