"""Exact Shapley attribution and bootstrap CIs."""

from __future__ import annotations

from fractions import Fraction as F
from itertools import combinations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blackbox.attribution import (
    PLAYERS,
    AttributionError,
    attribute,
    coalitions,
    label,
    means,
    shapley,
    shares,
)


def v_from(fn) -> dict:
    return {c: F(fn(c)) for c in coalitions()}


def run(samples, **kw):
    return attribute(
        samples,
        resamples=kw.get("resamples", 400),
        ci_level=F(95, 100),
        max_ci_width=kw.get("width", F(35, 100)),
        seed="case-1",
    )


def test_eight_coalitions():
    cs = coalitions()
    assert len(cs) == 8 and frozenset() in cs and frozenset(PLAYERS) in cs
    assert label(frozenset({"A", "U"})) == "do(U,A)" and label(frozenset()) == "observed"


def test_pure_user_fault():
    # Bad unless the user's wording is corrected.
    v = v_from(lambda c: 0 if "U" in c else 1)
    phi = shapley(v)
    assert phi == {"U": F(1), "M": F(0), "A": F(0)}
    assert shares(phi) == {"U": F(1), "M": F(0), "A": F(0)}


@pytest.mark.parametrize("player", PLAYERS)
def test_any_single_player_fault(player):
    v = v_from(lambda c: 0 if player in c else 1)
    sh = shares(shapley(v))
    assert sh[player] == 1 and sum(sh.values()) == 1


def test_conjunctive_fault_splits_evenly():
    # Bad unless BOTH U and M are corrected (the Kestrel shape): equal shares, A gets nothing.
    v = v_from(lambda c: 0 if {"U", "M"} <= c else 1)
    assert shares(shapley(v)) == {"U": F(1, 2), "M": F(1, 2), "A": F(0)}


def test_disjunctive_fault_splits_evenly():
    # Fixing either U or A is enough.
    v = v_from(lambda c: 0 if ("U" in c or "A" in c) else 1)
    assert shares(shapley(v)) == {"U": F(1, 2), "M": F(0), "A": F(1, 2)}


@settings(max_examples=200, deadline=None)
@given(st.lists(st.fractions(0, 1), min_size=8, max_size=8))
def test_efficiency_axiom(values):
    """Shapley values always sum to the total badness removed: v({}) - v(N)."""
    v = dict(zip(coalitions(), values, strict=True))
    assert sum(shapley(v).values()) == v[frozenset()] - v[frozenset(PLAYERS)]


@settings(max_examples=100, deadline=None)
@given(st.lists(st.fractions(0, 1), min_size=8, max_size=8))
def test_symmetry_axiom(values):
    """Swapping two players' roles in v swaps their Shapley values."""
    v = dict(zip(coalitions(), values, strict=True))
    swap = {"U": "M", "M": "U", "A": "A"}
    v2 = {frozenset(swap[p] for p in c): x for c, x in v.items()}
    a, b = shapley(v), shapley(v2)
    assert a["U"] == b["M"] and a["M"] == b["U"] and a["A"] == b["A"]


def test_null_player_gets_zero():
    v = v_from(lambda c: 0 if "U" in c else 1)
    assert shapley(v)["A"] == 0  # A never changes anything


def test_nothing_attributable():
    v = v_from(lambda c: 0)
    assert shares(shapley(v)) is None
    res = run({c: [0, 0, 0, 0] for c in coalitions()})
    assert res.shares is None and res.escalate and res.majority() is None and res.leaders() == []


def test_deterministic_samples_give_zero_width_intervals():
    samples = {c: [0] * 4 if "U" in c else [1] * 4 for c in coalitions()}
    res = run(samples)
    assert res.majority() == "U" and not res.escalate
    assert res.ci["U"] == (F(1), F(1)) and res.ci["M"] == (F(0), F(0))
    d = res.to_dict()
    assert d["shares"] == {"U": "1.0000", "M": "0.0000", "A": "0.0000"} and d["k"] == 4


def test_noisy_samples_widen_intervals_and_escalate():
    noisy = {c: [1, 0, 1, 0] for c in coalitions()}
    noisy[frozenset({"U"})] = [0, 0, 1, 0]
    res = run(noisy, width=F(1, 10))
    assert res.escalate and "above" in res.reason


def test_bootstrap_is_reproducible():
    noisy = {c: [1, 0, 1, 1] if "M" not in c else [0, 1, 0, 0] for c in coalitions()}
    assert run(noisy).ci == run(noisy).ci


def test_majority_tie_breaks_by_player_order():
    v = v_from(lambda c: 0 if {"U", "M"} <= c else 1)
    samples = {c: [int(v[c])] for c in coalitions()}
    res = run(samples)
    assert res.majority() == "U" and res.leaders() == ["U", "M"]


@pytest.mark.parametrize(
    "bad",
    [
        {c: [1] for c in coalitions()[:-1]},  # missing a coalition
        {**{c: [1] for c in coalitions()}, frozenset({"U"}): []},  # empty
        {**{c: [1] for c in coalitions()}, frozenset({"M"}): [2]},  # not binary
    ],
)
def test_invalid_samples(bad):
    with pytest.raises(AttributionError):
        shapley(means(bad))


def test_hand_computed_mixed_case():
    """v = {}:1, U:1/2, M:1, A:1, UM:0, UA:1/2, MA:1, UMA:0, worked by hand."""
    table = {
        (): 1,
        ("U",): F(1, 2),
        ("M",): 1,
        ("A",): 1,
        ("U", "M"): 0,
        ("U", "A"): F(1, 2),
        ("M", "A"): 1,
        ("U", "M", "A"): 0,
    }
    v = {frozenset(k): F(x) for k, x in table.items()}
    phi = shapley(v)
    # U: (1/3)(1/2) + (1/6)(1) + (1/6)(1/2) + (1/3)(1) = 3/4 ; M: 1/4 ; A: 0
    assert phi == {"U": F(3, 4), "M": F(1, 4), "A": F(0)}
    assert sum(phi.values()) == 1


def test_coalitions_are_all_subsets():
    expected = {frozenset(c) for r in range(4) for c in combinations(PLAYERS, r)}
    assert set(coalitions()) == expected


def test_paired_bootstrap_resamples_sample_indices_jointly():
    noisy = {c: [1, 0, 1, 1] if "M" not in c else [0, 1, 0, 0] for c in coalitions()}
    a = attribute(
        noisy, resamples=300, ci_level=F(95, 100), max_ci_width=F(1), seed="s", paired=True
    )
    b = attribute(
        noisy, resamples=300, ci_level=F(95, 100), max_ci_width=F(1), seed="s", paired=True
    )
    assert a.ci == b.ci and a.shares == run(noisy).shares
    with pytest.raises(AttributionError, match="same k"):
        uneven = {**noisy, frozenset(): [1, 1]}
        attribute(
            uneven, resamples=10, ci_level=F(95, 100), max_ci_width=F(1), seed="s", paired=True
        )
