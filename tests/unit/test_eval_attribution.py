"""Attribution-eval harness: round robin, planted preconditions, scoring, summary, forecast."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from eval.attribution import (
    Instance,
    _clarify,
    append,
    load_rows,
    next_instance,
    precondition,
    score,
    summarize,
    write_summary,
)
from paypal.config import load_yaml

CFG = load_yaml("eval/attribution.yaml")


def test_round_robin_balances_types_and_cycles_variants():
    first = [next_instance(CFG, i) for i in range(8)]
    assert [x.kind for x in first[:4]] == CFG["order"]
    assert [x.kind for x in first[4:]] == CFG["order"]
    assert first[0].variant == 0 and first[4].variant == 1
    assert next_instance(CFG, 4 * 3).variant == 0  # pure_user has 3 variants
    assert Instance("mixed", 2, 11).id == "mixed-v2-n11"


def test_every_variant_is_well_formed():
    for kind, sc in CFG["scenarios"].items():
        assert set(sc["expect"]["shares"]) == {"U", "M", "A"}, kind
        assert sum(sc["expect"]["shares"].values()) == 1, kind
        assert ("majority" in sc["expect"]) != ("top" in sc["expect"]), kind
        for v in sc["variants"]:
            assert v["request"].endswith(","), kind
            assert v["complaint"], kind
            assert kind in CFG["nightly"]["estimate_tokens"]


def test_clarifications_edit_the_signed_mandate_fields():
    base = {"required_attributes": {"size_us": "10"}, "forbidden_attributes": {}}
    assert _clarify(base, {"required.size_us": "10.5"}) == {
        "required_attributes": {"size_us": "10.5"}
    }
    assert _clarify(base, {"forbidden.material": "leather"}) == {
        "forbidden_attributes": {"material": ["leather"]}
    }
    with pytest.raises(ValueError):
        _clarify(base, {"preference.x": "y"})


CHOSEN = {"sku": "KES-001", "total": "90.52", "attributes": {"size_us": "10"}}


@pytest.mark.parametrize(
    ("check", "best", "ok"),
    [
        ({"sku": "KES-001"}, None, True),
        ({"sku": "KES-004"}, None, False),
        ({"size_us": "10"}, None, True),
        ({"size_us": "9.5"}, None, False),
        ({"overpaid": True}, Decimal("79.69"), True),
        ({"overpaid": True}, Decimal("90.52"), False),
        ({"overpaid": True}, None, False),
    ],
)
def test_planted_precondition(check, best, ok):
    assert (precondition(check, CHOSEN, best) is None) is ok


def att(shares, ci, majority):
    return {"shares": shares, "ci": ci, "majority": majority}


def test_scoring_pure_and_mixed_cases():
    pure = {"majority": "A", "shares": {"U": 0, "M": 0, "A": 1}}
    a = att(
        {"U": "0.0000", "M": "0.0000", "A": "1.0000"},
        {"U": ["0", "0.2"], "M": ["0", "0.2"], "A": ["0.74", "1"]},
        "A",
    )
    assert score(pure, a) == {
        "correct": True,
        "mae": 0.0,
        "covered": {"U": True, "M": True, "A": True},
    }
    mixed = {"top": ["U", "M"], "shares": {"U": 0.5, "M": 0.5, "A": 0}}
    b = att(
        {"U": "0.6000", "M": "0.3000", "A": "0.1000"},
        {"U": ["0.3", "0.66"], "M": ["0.31", "0.65"], "A": ["0.05", "0.2"]},
        "U",
    )
    s = score(mixed, b)
    assert s["correct"] and s["mae"] == pytest.approx(0.1333, abs=1e-4)
    assert s["covered"] == {"U": True, "M": True, "A": False}
    assert score(pure, None)["correct"] is False
    assert score(pure, {"shares": None})["why"] == "no attribution"


def test_results_append_and_summarize(tmp_path: Path):
    rows = [
        {
            "kind": "pure_agent",
            "valid": True,
            "correct": True,
            "mae": 0.0,
            "covered": {"U": True, "M": True, "A": True},
            "budget_tokens": 15000,
        },
        {
            "kind": "pure_agent",
            "valid": True,
            "correct": False,
            "mae": 0.5,
            "covered": {"U": True, "M": False, "A": False},
            "budget_tokens": 17000,
        },
        {
            "kind": "pure_agent",
            "valid": False,
            "invalid": "agent bought the cheapest",
            "budget_tokens": 3000,
        },
        {
            "kind": "mixed",
            "valid": True,
            "correct": True,
            "mae": 0.1,
            "covered": None,
            "budget_tokens": 26000,
        },
    ]
    for r in rows:
        append(r, tmp_path)
    assert load_rows(tmp_path) == [json.loads(json.dumps(r, sort_keys=True)) for r in rows]
    s = write_summary(tmp_path)
    assert s["pure_agent"] == {
        "instances": 3,
        "valid": 2,
        "accuracy": 0.5,
        "mean_abs_error": 0.25,
        "interval_coverage": 0.6667,
        "mean_budget_tokens": 11667,
    }
    assert s["mixed"]["interval_coverage"] is None
    assert s["overall"] == {"instances": 4, "valid": 3, "accuracy": 0.6667}
    assert json.loads((tmp_path / "summary.json").read_text())["overall"]["valid"] == 3
    assert load_rows(tmp_path / "none") == [] and summarize([])["overall"]["accuracy"] is None


def _runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "nightly_eval.py"
    spec = importlib.util.spec_from_file_location("nightly_eval", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_forecast_respects_the_nightly_allowance():
    f = _runner().forecast
    one = f(CFG, date(2026, 10, 7), date(2026, 10, 7), 0, None)
    # 100k a night: pure_user 28k + pure_merchant 4k + pure_agent 16k + mixed 28k = 76k,
    # then the next pure_user (28k) does not fit in the 24k left.
    assert one == {
        "nights": 1,
        "instances": 4,
        "by_kind": {"pure_user": 1, "pure_merchant": 1, "pure_agent": 1, "mixed": 1},
    }
    short = f(CFG, date(2026, 10, 7), date(2026, 10, 7), 0, 30000)
    assert short["instances"] == 1  # pure_user takes 28k; pure_merchant's 4k no longer fits
