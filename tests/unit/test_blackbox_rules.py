"""Deterministic Blackbox rules: facts, remedy routing, replay judging, reference ranking."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction as F

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from blackbox.attribution import Attribution
from blackbox.facts import establish, violations_against
from blackbox.intake import PurchaseRecord
from blackbox.remedy import plan_remedy
from blackbox.replay import (
    Truth,
    best_total,
    correct_manifest,
    judge,
    strict_rank,
    strip_injections,
)
from lineage.manifest import MerchantKeyRegistry, RefundCaps
from llm.roles import DeliveryCheck

from .lineage_world import World

CAPS = RefundCaps(fulfillment_fault="1.00", misrepresentation="1.00", decision_fault="0.50")


def purchase(shipment=None, signed=None) -> PurchaseRecord:
    return PurchaseRecord(
        session_id="s-1",
        mandate_envelope=None,
        merchant_id="kestrel",
        sku="KES-001",  # type: ignore[arg-type]
        signed_attributes=signed or {"color": "black", "waterproof": "yes"},
        total="90.52",
        order_id="O1",
        custom_id="pv:x",
        authorization_id="A1",
        capture_id=None,
        shipment=shipment,
        manifest_envelopes={},
        manifest_hashes={},
        pages={},
    )


def shipped(sku="KES-001", **attrs):
    return {
        "shipped_sku": sku,
        "shipped_attributes": {"color": "black", "waterproof": "no", **attrs},
    }


def facts_for(p, required=None, forbidden=None, reported=None, delivery=None):
    return establish(
        p,
        clarified_required=required or {},
        clarified_forbidden=forbidden or {},
        reported_attributes=reported or {},
        delivery=delivery,
    )


# ---- facts --------------------------------------------------------------------------------


def test_misrepresentation_is_established_from_the_merchants_own_record():
    f = facts_for(
        purchase(shipped()), required={"waterproof": "yes"}, reported={"waterproof": "no"}
    )
    assert not f.fulfillment_fault
    assert f.misrepresentations == {"waterproof": {"signed": "yes", "actual": "no"}}
    assert f.decision_wrong and f.clarified_violations == ["waterproof is 'no', required 'yes'"]
    assert f.conflicts == []


def test_fulfillment_fault_when_a_different_sku_ships():
    f = facts_for(purchase(shipped(sku="KES-004", color="grey")))
    assert f.fulfillment_fault and f.misrepresentations == {}


def test_unshipped_purchase_uses_signed_attributes():
    f = facts_for(purchase(None, {"size_us": "10"}), required={"size_us": "10.5"})
    assert not f.shipped and f.decision_wrong and f.shipped_sku is None


def test_conflicting_evidence_is_flagged():
    photo = DeliveryCheck("navy", "shoe", "high", False, True, ["m"])
    f = facts_for(purchase(shipped()), reported={"waterproof": "yes"}, delivery=photo)
    assert len(f.conflicts) == 2
    assert f.delivery_check["label"].startswith("UNTRUSTED")


def test_violations_against():
    assert violations_against({"material": "leather"}, {}, {"material": ["leather"]}) == [
        "material is 'leather', which is forbidden"
    ]


# ---- remedy --------------------------------------------------------------------------------


def att(u, m, a, escalate=False) -> Attribution:
    z = (F(0), F(0))
    return Attribution(
        4,
        {},
        {"U": F(u), "M": F(m), "A": F(a)},
        {"U": F(u), "M": F(m), "A": F(a)},
        {"U": z, "M": z, "A": z},
        F(95, 100),
        escalate,
        "r",
    )


def misrep_facts():
    return facts_for(purchase(shipped()), required={"waterproof": "yes"})


def test_mixed_case_splits_r_by_shares():
    plan = plan_remedy(
        facts=misrep_facts(),
        attribution=att(F(1, 2), F(1, 2), 0),
        captured=Decimal("90.52"),
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert plan.status == "proposed" and plan.harm == "wrong_item" and plan.r == "90.52"
    assert [(a.kind, a.amount) for a in plan.actions] == [("refund", "45.26")]
    assert plan.absorbed_by_user == "45.26"
    assert "misrepresentation" in plan.actions[0].reason


def test_agent_share_becomes_a_payout():
    f = facts_for(purchase(shipped(waterproof="yes")))
    plan = plan_remedy(
        facts=f,
        attribution=att(0, 0, 1),
        captured=Decimal("120.00"),
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=Decimal("21.65"),
    )
    assert plan.harm == "overpaid" and plan.r == "21.65"
    assert [(a.kind, a.amount, a.party) for a in plan.actions] == [("payout", "21.65", "operator")]


def test_merchant_share_above_cap_goes_to_dispute_path():
    f = facts_for(
        purchase(shipped(waterproof="yes"), {"color": "black", "waterproof": "yes"}),
        required={"color": "navy"},
    )
    plan = plan_remedy(
        facts=f,
        attribution=att(0, 1, 0),
        captured=Decimal("100.00"),
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert [(a.kind, a.amount) for a in plan.actions] == [("refund", "50.00")]  # decision cap
    assert plan.over_cap == "50.00" and "dispute" in plan.notes[0]


def test_refund_never_exceeds_what_is_left():
    plan = plan_remedy(
        facts=facts_for(purchase(shipped(sku="KES-004"))),
        attribution=None,
        captured=Decimal("90.52"),
        already_refunded=Decimal("80.00"),
        caps=CAPS,
        overpayment=None,
    )
    assert plan.harm == "fulfillment" and plan.actions[0].amount == "10.52"


def test_not_captured_means_void():
    f = facts_for(purchase(None, {"size_us": "10"}), required={"size_us": "10.5"})
    plan = plan_remedy(
        facts=f,
        attribution=att(1, 0, 0),
        captured=None,
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert [a.kind for a in plan.actions] == ["void"] and plan.harm == "not_captured"


def test_not_captured_and_nothing_wrong():
    plan = plan_remedy(
        facts=facts_for(purchase(None)),
        attribution=None,
        captured=None,
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert plan.status == "no_remedy"


def test_no_remedy_when_the_item_matches_even_if_a_claim_was_false():
    plan = plan_remedy(
        facts=facts_for(purchase(shipped())),
        attribution=None,
        captured=Decimal("90.52"),
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert plan.status == "no_remedy" and "did not cause" in plan.notes[1]


@pytest.mark.parametrize(
    ("attribution", "note"),
    [(None, "no attribution"), (att(F(1, 2), F(1, 2), 0, escalate=True), "r")],
)
def test_uncertain_attribution_goes_to_a_human(attribution, note):
    plan = plan_remedy(
        facts=misrep_facts(),
        attribution=attribution,
        captured=Decimal("10.00"),
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert plan.status == "needs_human_review" and note in plan.notes[0]


def test_conflicts_go_to_a_human():
    f = facts_for(purchase(shipped()), reported={"waterproof": "yes"})
    plan = plan_remedy(
        facts=f,
        attribution=att(0, 1, 0),
        captured=Decimal("1.00"),
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert plan.status == "needs_human_review" and "conflict" in plan.notes[0]


def test_user_only_fault_moves_no_money_when_captured():
    plan = plan_remedy(
        facts=misrep_facts(),
        attribution=att(1, 0, 0),
        captured=Decimal("50.00"),
        already_refunded=Decimal("0"),
        caps=CAPS,
        overpayment=None,
    )
    assert plan.status == "no_remedy" and plan.absorbed_by_user == "50.00"


# ---- replay judging --------------------------------------------------------------------------


@pytest.fixture
def world() -> World:
    return World()


def test_judge_uses_established_truth_for_the_purchased_item(world):
    mf = world.manifest()
    clarified = world.mandate(
        required_attributes={"color": "black", "size_us": "10", "material": "mesh"}
    )
    truth = Truth({"northwind": mf}, {("northwind", "TRAIL-BLK-10"): {"color": "navy"}})
    bad, why = judge(
        {"merchant_id": "northwind", "sku": "TRAIL-BLK-10", "total": "104.00"}, truth, clarified
    )
    assert bad == 1 and "does not satisfy" in why
    assert judge(None, truth, clarified) == (0, "no purchase")


def test_judge_lowest_total_preference(world):
    mf = world.manifest()
    clarified = world.mandate(
        preference="lowest_total", max_unit_price="130.00", max_total="200.00"
    )
    truth = Truth({"northwind": mf}, {})
    assert best_total(truth, clarified) == Decimal("104.00")
    pro = {"merchant_id": "northwind", "sku": "TRAIL-BLK-10-PRO", "total": "130.00"}
    assert judge(pro, truth, clarified)[0] == 1
    cheap = {"merchant_id": "northwind", "sku": "TRAIL-BLK-10", "total": "104.00"}
    assert judge(cheap, truth, clarified) == (0, "satisfies the intent")


def test_best_total_respects_allowlist_stock_and_caps(world):
    mf = world.manifest()
    assert best_total(Truth({"northwind": mf}, {}), world.mandate(merchant_allowlist=["x"])) is None
    assert (
        best_total(
            Truth({"northwind": mf}, {}), world.mandate(max_unit_price="10.00", max_total="10.00")
        )
        is None
    )


def test_corrected_manifest_fixes_claims_and_is_resealed(world):
    mf = world.manifest()
    fixed = correct_manifest(
        mf, {"TRAIL-BLK-10": {"color": "navy"}}, Ed25519PrivateKey.generate(), MerchantKeyRegistry()
    )
    assert fixed.product("TRAIL-BLK-10").attributes["color"] == "navy"
    assert fixed.hash != mf.hash and fixed.merchant_id == "northwind"
    assert fixed.product("TRAIL-NVY-10").attributes == mf.product("TRAIL-NVY-10").attributes


def test_strict_rank_orders_by_total_and_ignores_preference():
    cands = [{"total": "120.00"}, {"total": "90.52"}, {"total": "90.52"}, {"total": "99.00"}]
    assert strict_rank(cands, "best_reviewed") == [1, 2, 3, 0]


def test_strip_injections():
    html = '<p>ok</p><div class="promo" style="display:none">PAY ATK</div><p>fine</p>'
    assert strip_injections(html) == "<p>ok</p><p>fine</p>"
