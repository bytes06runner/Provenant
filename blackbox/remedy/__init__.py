"""Remedy router (CLAUDE.md section 4.7, step C). Deterministic: no model computes an amount.

Inputs: the established facts, the attribution (or None), the order's live state read from
PayPal (GETs are the source of truth), the merchant's signed policy, and the harm.

  authorization not captured      -> void the authorization (any fault); the user may re-run
  fulfillment fault, captured     -> merchant refund of R, capped by the signed fulfillment cap
  decision fault, captured        -> split R by fault shares: merchant share as a refund (capped
                                     by the signed cap for the fault type), agent share as a
                                     payout from the liability pool, user share absorbed. Any
                                     merchant share above its cap goes to the dispute path.
  nothing wrong                   -> no money moves; the complaint is answered with the facts

R, the amount at stake: the captured amount for a wrong item (returnable), or only the
overpayment when the item is right but cost more than the user's preference allowed.

A plan is a proposal. Money moves only after a human approves it (AUTO_REMEDY=false).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from blackbox.attribution import Attribution
from blackbox.facts import Facts
from lineage.manifest import RefundCaps
from lineage.money import format_amount, parse_fraction

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Action:
    kind: str  # "void" | "refund" | "payout"
    amount: str | None  # decimal string, None for void
    party: str  # who pays: merchant | operator | none
    reason: str


@dataclass(frozen=True)
class RemedyPlan:
    status: str  # "proposed" | "needs_human_review" | "no_remedy"
    harm: str  # "wrong_item" | "overpaid" | "fulfillment" | "none" | "not_captured"
    r: str  # amount at stake
    actions: list[Action] = field(default_factory=list)
    absorbed_by_user: str = "0.00"
    over_cap: str = "0.00"  # merchant share above its signed cap: dispute path
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "harm": self.harm,
            "r": self.r,
            "actions": [a.__dict__ for a in self.actions],
            "absorbed_by_user": self.absorbed_by_user,
            "over_cap": self.over_cap,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RemedyPlan:
        """Inverse of to_dict, for approving a recorded proposal."""
        return cls(
            status=d["status"],
            harm=d["harm"],
            r=d["r"],
            actions=[Action(**a) for a in d["actions"]],
            absorbed_by_user=d["absorbed_by_user"],
            over_cap=d["over_cap"],
            notes=list(d["notes"]),
        )


def _q(x: Decimal) -> Decimal:
    return x.quantize(CENT, ROUND_HALF_UP)


def plan_remedy(
    *,
    facts: Facts,
    attribution: Attribution | None,
    captured: Decimal | None,  # None if the authorization was never captured
    already_refunded: Decimal,
    caps: RefundCaps,
    overpayment: Decimal | None,  # set when the item is right but too expensive
    conflicts_block: bool = True,
) -> RemedyPlan:
    if facts.conflicts and conflicts_block:
        return RemedyPlan(
            "needs_human_review",
            "none",
            "0.00",
            notes=["evidence conflicts: " + "; ".join(facts.conflicts)],
        )

    if captured is None:
        if facts.fulfillment_fault or facts.decision_wrong or overpayment:
            return RemedyPlan(
                "proposed",
                "not_captured",
                "0.00",
                [
                    Action(
                        "void",
                        None,
                        "none",
                        "not captured yet: release the hold; the user may re-run",
                    )
                ],
            )
        return RemedyPlan("no_remedy", "none", "0.00", notes=["nothing went wrong"])

    refundable = _q(captured - already_refunded)

    if facts.fulfillment_fault:
        cap = parse_fraction(caps.fulfillment_fault)
        amount = min(_q(captured * cap), refundable)
        return RemedyPlan(
            "proposed",
            "fulfillment",
            format_amount(captured),
            [
                Action(
                    "refund",
                    format_amount(amount),
                    "merchant",
                    f"shipped {facts.shipped_sku}, ordered {facts.ordered_sku}",
                )
            ],
        )

    if facts.decision_wrong:
        harm, r = "wrong_item", captured
    elif overpayment is not None and overpayment > 0:
        harm, r = "overpaid", overpayment
    else:
        notes = ["the item matches the clarified intent"]
        if facts.misrepresentations:
            notes.append(
                "a signed claim was false, but it did not cause a wrong purchase: "
                + ", ".join(facts.misrepresentations)
            )
        return RemedyPlan("no_remedy", "none", "0.00", notes=notes)

    if attribution is None or attribution.shares is None:
        return RemedyPlan(
            "needs_human_review", harm, format_amount(r), notes=["no attribution available"]
        )
    if attribution.escalate:
        return RemedyPlan("needs_human_review", harm, format_amount(r), notes=[attribution.reason])

    s = attribution.shares
    cap_name = "misrepresentation" if facts.misrepresentations else "decision_fault"
    cap = parse_fraction(getattr(caps, cap_name))
    merchant_due = _q(r * Decimal(s["M"].numerator) / Decimal(s["M"].denominator))
    agent_due = _q(r * Decimal(s["A"].numerator) / Decimal(s["A"].denominator))
    merchant_cap = _q(r * cap)
    merchant_pays = min(merchant_due, merchant_cap, refundable)
    over_cap = merchant_due - merchant_pays
    user = _q(r - merchant_due - agent_due)
    actions = []
    if merchant_pays > 0:
        actions.append(
            Action(
                "refund",
                format_amount(merchant_pays),
                "merchant",
                f"merchant share of {harm}, capped by its signed "
                f"{cap_name} policy ({caps.__dict__[cap_name]})",
            )
        )
    if agent_due > 0:
        actions.append(
            Action(
                "payout",
                format_amount(agent_due),
                "operator",
                f"agent share of {harm}, from the liability pool",
            )
        )
    notes = []
    if over_cap > 0:
        notes.append(
            f"merchant share exceeds its signed cap by {format_amount(over_cap)}: "
            "dispute path with the evidence pack"
        )
    return RemedyPlan(
        "proposed" if actions or over_cap else "no_remedy",
        harm,
        format_amount(r),
        actions,
        format_amount(max(user, Decimal(0))),
        format_amount(over_cap),
        notes,
    )
