"""Fact establishment (CLAUDE.md section 4.7, step A). No LLM judges money here.

Inputs: the purchase record, the merchant's shipment record, the user's confirmed observations,
an optional photo check (vision is UNTRUSTED user evidence, used only to corroborate), and the
user's clarified intent.

  fulfillment fault   the merchant shipped a different sku than was ordered -> merchant owns 100%
  misrepresentation   the right sku shipped, but an attribute the merchant SIGNED is false.
                      Provable: the signed manifest is in the recorder with the merchant's key.
  decision wrong      the item actually received violates the clarified intent (required or
                      forbidden attributes). Preference (for example lowest total) is judged in
                      replay, where the alternatives are known.
  conflicts           evidence that disagrees (photo vs shipment record, user report vs record).
                      Any conflict sends the case to a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from blackbox.intake import PurchaseRecord
from llm.roles import DeliveryCheck


@dataclass(frozen=True)
class Facts:
    ordered_sku: str
    shipped_sku: str | None
    signed_attributes: dict[str, str]
    actual_attributes: dict[str, str]
    fulfillment_fault: bool
    misrepresentations: dict[str, dict[str, str]]  # attr -> {"signed": .., "actual": ..}
    clarified_violations: list[str]
    shipped: bool
    delivery_check: dict[str, Any] | None
    conflicts: list[str] = field(default_factory=list)

    @property
    def decision_wrong(self) -> bool:
        return bool(self.clarified_violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordered_sku": self.ordered_sku,
            "shipped_sku": self.shipped_sku,
            "shipped": self.shipped,
            "signed_attributes": self.signed_attributes,
            "actual_attributes": self.actual_attributes,
            "fulfillment_fault": self.fulfillment_fault,
            "misrepresentations": self.misrepresentations,
            "clarified_violations": self.clarified_violations,
            "decision_wrong": self.decision_wrong,
            "delivery_check": self.delivery_check,
            "conflicts": self.conflicts,
        }


def violations_against(
    attributes: dict[str, str], required: dict[str, str], forbidden: dict[str, list[str]]
) -> list[str]:
    out = [
        f"{k} is {attributes.get(k)!r}, required {v!r}"
        for k, v in required.items()
        if attributes.get(k) != v
    ]
    out += [
        f"{k} is {attributes[k]!r}, which is forbidden"
        for k, banned in forbidden.items()
        if attributes.get(k) in banned
    ]
    return out


def establish(
    purchase: PurchaseRecord,
    *,
    clarified_required: dict[str, str],
    clarified_forbidden: dict[str, list[str]],
    reported_attributes: dict[str, str],
    delivery: DeliveryCheck | None,
) -> Facts:
    conflicts: list[str] = []
    shipment = purchase.shipment
    signed = dict(purchase.signed_attributes)

    if shipment is None:
        # Not shipped yet: the item is what was ordered, as signed, plus anything the user knows.
        actual = {**signed, **reported_attributes}
        return Facts(
            ordered_sku=purchase.sku,
            shipped_sku=None,
            signed_attributes=signed,
            actual_attributes=actual,
            fulfillment_fault=False,
            misrepresentations={},
            clarified_violations=violations_against(
                actual, clarified_required, clarified_forbidden
            ),
            shipped=False,
            delivery_check=None,
            conflicts=conflicts,
        )

    shipped_sku = str(shipment["shipped_sku"])
    record_attrs = dict(shipment["shipped_attributes"])
    for k, v in reported_attributes.items():
        if k in record_attrs and record_attrs[k] != v:
            conflicts.append(f"user reports {k}={v!r}, merchant record says {record_attrs[k]!r}")
    if delivery is not None and delivery.primary_color != record_attrs.get("color"):
        conflicts.append(
            f"photo shows {delivery.primary_color!r}, merchant record says "
            f"{record_attrs.get('color')!r}"
        )
    actual = {**record_attrs, **reported_attributes}
    fulfillment_fault = shipped_sku != purchase.sku
    misrep: dict[str, dict[str, str]] = {}
    if not fulfillment_fault:
        misrep = {
            k: {"signed": v, "actual": actual[k]}
            for k, v in signed.items()
            if k in actual and actual[k] != v
        }
    return Facts(
        ordered_sku=purchase.sku,
        shipped_sku=shipped_sku,
        signed_attributes=signed,
        actual_attributes=actual,
        fulfillment_fault=fulfillment_fault,
        misrepresentations=misrep,
        clarified_violations=violations_against(actual, clarified_required, clarified_forbidden),
        shipped=True,
        delivery_check=(
            {
                "primary_color": delivery.primary_color,
                "confidence": delivery.confidence,
                "escalated": delivery.escalated,
                "models": delivery.models,
                "label": "UNTRUSTED (user evidence)",
            }
            if delivery
            else None
        ),
        conflicts=conflicts,
    )
