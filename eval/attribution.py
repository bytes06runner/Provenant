"""Attribution-accuracy evaluation (CLAUDE.md section 8.3) on planted ground-truth faults.

One instance = one real purchase through Lineage (stopping before a PayPal order), an optional
simulated shipment, a complaint, and Blackbox's assessment (facts, replay, attribution) with real
models. Scenarios and budgets: config/eval/attribution.yaml. Results append, one JSON object per
line, to eval/results/attribution/results.jsonl; `summarize` rebuilds summary.json from them.

Scoring is deterministic: pure cases are correct when the majority player is the planted one,
mixed cases when the top players are the planted set. Every row also carries the mean absolute
error of the shares and whether each planted share lies inside its interval.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from blackbox.case import CaseDeps, assess
from blackbox.intake import Complaint, clarify_edits, file_complaint, load_purchase
from lineage.fulfillment import ship
from llm import roles

RESULTS = Path(__file__).resolve().parent / "results" / "attribution"


@dataclass(frozen=True)
class Instance:
    kind: str  # pure_user | pure_merchant | pure_agent | mixed
    variant: int
    seq: int  # position in the round robin, for a stable instance id

    @property
    def id(self) -> str:
        return f"{self.kind}-v{self.variant}-n{self.seq}"


def next_instance(cfg: dict[str, Any], done: int) -> Instance:
    """The `done`-th instance of the round robin over types, cycling each type's variants."""
    order = cfg["order"]
    kind = order[done % len(order)]
    rounds = done // len(order)
    variants = cfg["scenarios"][kind]["variants"]
    return Instance(kind, rounds % len(variants), done)


def _fmt(value: Any, budget: str) -> Any:
    return value.format(budget=budget) if isinstance(value, str) else value


def precondition(check: dict[str, Any], chosen: dict[str, Any], best: Decimal | None) -> str | None:
    """None if the planted fault really happened, else why the instance is invalid."""
    if "sku" in check and chosen["sku"] != check["sku"]:
        return f"agent bought {chosen['sku']}, not the planted {check['sku']}"
    if "size_us" in check and chosen["attributes"].get("size_us") != check["size_us"]:
        return f"agent bought size {chosen['attributes'].get('size_us')}"
    if check.get("overpaid") and (best is None or Decimal(chosen["total"]) <= best):
        return f"agent bought the cheapest available ({chosen['total']})"
    return None


def score(expect: dict[str, Any], att: dict[str, Any] | None) -> dict[str, Any]:
    if att is None or not att.get("shares"):
        return {"correct": False, "mae": None, "covered": None, "why": "no attribution"}
    shares = {p: float(x) for p, x in att["shares"].items()}
    truth = {p: float(x) for p, x in expect["shares"].items()}
    mae = sum(abs(shares[p] - truth[p]) for p in truth) / len(truth)
    covered = {p: float(att["ci"][p][0]) <= truth[p] <= float(att["ci"][p][1]) for p in truth}
    if "majority" in expect:
        correct = att.get("majority") == expect["majority"]
    else:
        top = sorted(shares, key=lambda p: -shares[p])[: len(expect["top"])]
        correct = set(top) == set(expect["top"])
    return {"correct": correct, "mae": round(mae, 4), "covered": covered}


@dataclass
class EvalDeps:
    """What an instance needs. Built from lineage.runtime.Runtime by the scripts."""

    new_session: Callable[..., Any]
    case_deps: CaseDeps
    records: dict[str, Any]  # merchant id -> record with .base_url
    http: Any
    addresses: list[str]
    vocabulary: dict[str, list[str]]
    photo: Callable[[str], bytes]
    tokens_used: Callable[[], int]  # tokens spent today on the budget model


def run_instance(cfg: dict[str, Any], inst: Instance, deps: EvalDeps) -> dict[str, Any]:
    sc = cfg["scenarios"][inst.kind]
    var = sc["variants"][inst.variant]
    budget = str(var.get("budget", cfg["defaults"]["budget"]))
    started, tokens_before = time.monotonic(), deps.tokens_used()
    row: dict[str, Any] = {
        "instance": inst.id,
        "kind": inst.kind,
        "variant": inst.variant,
        "planted": sc["planted"],
        "expect": sc["expect"],
        "k": cfg["k"],
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    s = deps.new_session(cfg["user"], rank_prompt=sc["rank_prompt"])
    row["session"] = s.session_id
    request = f"{var['request']} {_fmt(cfg['defaults']['suffix'], budget)}"
    proposal = s.propose(request, deps.addresses)
    fields = dict(proposal.fields)
    for key, value in cfg["defaults"]["answers"].items():
        if fields.get(key) is None:
            fields[key] = _fmt(value, budget)
    fields.update(var.get("answers", {}))
    problems = roles._check_proposal(  # noqa: SLF001
        fields, deps.vocabulary, deps.addresses, sorted(deps.records)
    )
    if problems:
        return _finish(row, started, tokens_before, deps, invalid=f"mandate: {problems}")
    vm = s.confirm_and_sign(fields)
    review = var.get("review_sku")
    url = f"{deps.records[review['merchant']].base_url}/reviews/{review['sku']}" if review else None
    p = s.run(vm, s.plan(vm, url))
    if not p.result.allowed:
        return _finish(row, started, tokens_before, deps, invalid="no compliant purchase")
    chosen = p.candidate.summary()
    row["chosen"] = {k: chosen[k] for k in ("merchant_id", "sku", "total")}
    early = precondition(
        {k: v for k, v in var.get("check", {}).items() if k != "overpaid"}, chosen, None
    )
    if early:  # checked before the replay, so an invalid instance costs no replay budget
        return _finish(row, started, tokens_before, deps, invalid=early)

    shipment = None
    if sc.get("fulfill"):
        shipment = ship(
            recorder=deps.case_deps.recorder,
            session_id=s.session_id,
            merchant_id=chosen["merchant_id"],
            order_ref=f"eval-{s.session_id}",
            sku=chosen["sku"],
            storefront_url=deps.records[chosen["merchant_id"]].base_url,
            http=deps.http,
            force_wrong_variant=bool(sc.get("planted_wrong_variant")),
        )
    purchase = load_purchase(deps.case_deps.recorder, s.session_id, require_order=False)
    base = vm.mandate.model_dump()
    complaint = Complaint(
        text=var["complaint"],
        clarified=clarify_edits(base, var.get("clarify", {})),
        reported_attributes=dict(var.get("report", {})),
        photo_png=deps.photo(shipment["shipped_attributes"]["color"])
        if sc.get("photo") and shipment
        else None,
    )
    case = file_complaint(deps.case_deps.recorder, purchase, complaint)
    row["case"] = case

    def record(event_type: str, payload: dict[str, Any]) -> None:
        deps.case_deps.recorder.append(case, event_type, payload)

    deps.case_deps.record_into(record)
    a = assess(deps.case_deps, purchase, case, complaint, record, k=int(cfg["k"]))
    best = next(
        (
            e.payload.get("best_total")
            for e in deps.case_deps.recorder.events(case)
            if e.event_type == "outcome.observed"
        ),
        None,
    )
    why = precondition(var.get("check", {}), chosen, Decimal(best) if best else None)
    if why:
        return _finish(row, started, tokens_before, deps, invalid=why)
    att = a.attribution.to_dict() if a.attribution else None
    row["attribution"] = att
    row["note"] = a.note or None
    row.update(score(sc["expect"], att))
    record(  # the recorder's canonical JSON takes decimal strings, not floats
        "eval.scored",
        {
            "instance": row["instance"],
            "expect_majority": sc["expect"].get("majority"),
            "expect_top": sc["expect"].get("top"),
            "correct": row["correct"],
            "mae": None if row["mae"] is None else f"{row['mae']:.4f}",
        },
    )
    return _finish(row, started, tokens_before, deps)


def _finish(
    row: dict[str, Any],
    started: float,
    tokens_before: int,
    deps: EvalDeps,
    invalid: str | None = None,
) -> dict[str, Any]:
    row["valid"] = invalid is None
    if invalid:
        row["invalid"] = invalid
    row["seconds"] = round(time.monotonic() - started, 1)
    row["budget_tokens"] = deps.tokens_used() - tokens_before
    return row


def append(row: dict[str, Any], results: Path = RESULTS) -> None:
    results.mkdir(parents=True, exist_ok=True)
    with (results / "results.jsonl").open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def load_rows(results: Path = RESULTS) -> list[dict[str, Any]]:
    path = results / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = by.setdefault(
            r["kind"],
            {"instances": 0, "valid": 0, "correct": 0, "mae": [], "coverage": [], "tokens": []},
        )
        b["instances"] += 1
        b["tokens"].append(r.get("budget_tokens", 0))
        if not r.get("valid"):
            continue
        b["valid"] += 1
        b["correct"] += int(bool(r.get("correct")))
        if r.get("mae") is not None:
            b["mae"].append(r["mae"])
        if r.get("covered"):
            b["coverage"].extend(r["covered"].values())
    out: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    for kind, b in sorted(by.items()):
        out[kind] = {
            "instances": b["instances"],
            "valid": b["valid"],
            "accuracy": round(b["correct"] / b["valid"], 4) if b["valid"] else None,
            "mean_abs_error": round(sum(b["mae"]) / len(b["mae"]), 4) if b["mae"] else None,
            "interval_coverage": round(sum(b["coverage"]) / len(b["coverage"]), 4)
            if b["coverage"]
            else None,
            "mean_budget_tokens": round(sum(b["tokens"]) / len(b["tokens"])),
        }
    valid = [r for r in rows if r.get("valid")]
    out["overall"] = {
        "instances": len(rows),
        "valid": len(valid),
        "accuracy": round(sum(bool(r.get("correct")) for r in valid) / len(valid), 4)
        if valid
        else None,
    }
    return out


def write_summary(results: Path = RESULTS) -> dict[str, Any]:
    summary = summarize(load_rows(results))
    results.mkdir(parents=True, exist_ok=True)
    (results / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
