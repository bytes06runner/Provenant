"""Label lattice, propagation, and the no-upgrade rule."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lineage.labels import (
    MINT,
    Label,
    Labeled,
    Source,
    derive,
    join,
    mint_merchant_signed,
    mint_user,
    untrusted,
)

labels = st.sampled_from(list(Label))


def test_lattice_order():
    assert Label.USER > Label.MERCHANT_SIGNED > Label.DERIVED > Label.UNTRUSTED


@given(st.lists(labels, min_size=1))
def test_join_is_least_trusted(ls):
    assert join(*ls) == min(ls)


@given(labels, labels, labels)
def test_join_is_commutative_associative_idempotent(a, b, c):
    assert join(a, b) == join(b, a)
    assert join(join(a, b), c) == join(a, join(b, c))
    assert join(a, a) == a


def test_join_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        join()


def test_signed_times_user_is_derived_with_both_sources():
    price = mint_merchant_signed(MINT, Decimal("12.50"), "manifest:nw:abc", "catalog[S1].price")
    qty = mint_user(MINT, 2, "mandate:h1", "quantity")
    total = derive(lambda p, q: p * q, price, qty)
    assert total.value == Decimal("25.00")
    assert total.label is Label.DERIVED
    assert total.source_refs() == {"manifest:nw:abc", "mandate:h1"}


def test_any_untrusted_input_taints_the_result():
    price = mint_merchant_signed(MINT, Decimal("12.50"), "manifest:nw:abc")
    promo = untrusted(Decimal("1"), "page:https://shop/p1", digest="d1")
    out = derive(lambda p, m: p * m, price, promo)
    assert out.label is Label.UNTRUSTED
    assert "page:https://shop/p1" in out.source_refs()


@given(st.lists(labels, min_size=1, max_size=6))
def test_derive_never_exceeds_derived_or_weakest_input(ls):
    inputs = [_any(lbl, i) for i, lbl in enumerate(ls)]
    out = derive(lambda *vs: len(vs), *inputs)
    assert out.label == join(Label.DERIVED, *ls)
    assert out.label <= Label.DERIVED
    assert out.sources == frozenset().union(*(i.sources for i in inputs))


def _any(label: Label, i: int) -> Labeled[int]:
    if label is Label.USER:
        return mint_user(MINT, i, f"mandate:{i}")
    if label is Label.MERCHANT_SIGNED:
        return mint_merchant_signed(MINT, i, f"manifest:m:{i}")
    if label is Label.DERIVED:
        return derive(lambda v: v, mint_user(MINT, i, f"mandate:{i}"))
    return untrusted(i, f"page:{i}")


@pytest.mark.parametrize("label", [Label.USER, Label.MERCHANT_SIGNED])
def test_trusted_labels_cannot_be_constructed_directly(label):
    with pytest.raises(PermissionError):
        Labeled("acct-attacker", label, frozenset({Source(label, "llm-output")}))


def test_cannot_claim_more_trust_than_sources():
    with pytest.raises(ValueError):
        Labeled("x", Label.DERIVED, frozenset({Source(Label.UNTRUSTED, "page:p")}))


def test_value_must_have_a_source():
    with pytest.raises(ValueError):
        Labeled("x", Label.UNTRUSTED, frozenset())


def test_labeled_values_are_immutable():
    v = untrusted("x", "page:p")
    with pytest.raises(AttributeError):
        v.label = Label.USER  # type: ignore[misc]


def test_untrusted_value_equal_to_trusted_value_is_still_untrusted():
    """Same content, different provenance: provenance wins."""
    a = mint_merchant_signed(MINT, "PAYEE-1", "manifest:nw:h")
    b = untrusted("PAYEE-1", "page:https://evil")
    assert a.value == b.value and a.label != b.label


def test_only_verifiers_hold_the_mint_capability():
    """MINT may be imported only by the mandate/manifest verifiers (and these tests)."""
    root = Path(__file__).resolve().parents[2]
    allowed = {
        root / "lineage" / "labels" / "__init__.py",
        root / "lineage" / "mandate" / "__init__.py",
        root / "lineage" / "manifest" / "__init__.py",
        root / "lineage" / "vault" / "__init__.py",
    }
    offenders = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or "tests" in path.parts or path in allowed:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "MINT":
                offenders.append(str(path.relative_to(root)))
            if isinstance(node, ast.alias) and node.name == "MINT":
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
