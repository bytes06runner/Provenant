"""LLM roles. Each turns a model call into a validated, typed result or a clear failure.

  propose_mandate  user's words -> proposed mandate fields plus ambiguity questions
  make_plan        confirmed mandate -> validated plan (one repair round with the validator's error)
  extract          untrusted text -> schema-valid JSON, or None (Q-LLM: no tools, no mandate)
  rank             candidate summaries + untrusted evidence -> candidate indices

Models never decide whether anything is allowed: mandates are confirmed and signed by the user,
plans are validated and then run by the deterministic interpreter, and extractions are always
UNTRUSTED data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from lineage.dsl import SCHEMA_PATH, Plan, PlanError, validate_plan
from llm.types import LLMRequest, LLMResponse, Message

PROMPTS = Path(__file__).resolve().parent / "prompts"


class Router(Protocol):
    def call(self, role: str, request: LLMRequest, *, sample_index: int = 0) -> LLMResponse: ...

    @property
    def config(self) -> Any: ...


class RoleError(Exception):
    pass


@cache
def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


@cache
def schema(name: str) -> dict[str, Any]:
    if name == "plan":
        return dict(json.loads(SCHEMA_PATH.read_text()))
    path = PROMPTS / f"{name}.schema.json"
    if not path.exists():
        raise RoleError(f"unknown extraction schema {name!r}")
    return dict(json.loads(path.read_text()))


def parse_json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return json.loads(t)


def _request(
    router: Router,
    role: str,
    system: str,
    user: str,
    schema_name: str,
    *,
    seed: int | None = None,
) -> LLMRequest:
    defaults = router.config.call_defaults(role)
    mode = defaults.get("structured", "schema")
    sch = schema(schema_name)
    if mode == "json":
        system = f"{system}\n\n## Output JSON schema\n\n{json.dumps(sch)}"
    return LLMRequest(
        messages=(Message("system", system), Message("user", user)),
        temperature=str(defaults.get("temperature", "0")),
        max_tokens=int(defaults.get("max_tokens", 1024)),
        seed=seed,
        json_schema=sch,
        schema_name=schema_name,
        structured=mode,
    )


# ---- mandate --------------------------------------------------------------------------


@dataclass(frozen=True)
class MandateProposal:
    fields: dict[str, Any]
    questions: list[str]
    problems: list[str]  # deterministic checks the proposal failed (also shown to the user)

    @property
    def ready(self) -> bool:
        return not self.questions and not self.problems


def propose_mandate(
    router: Router,
    user_text: str,
    *,
    vocabulary: dict[str, list[str]],
    category: str,
    address_refs: list[str],
    merchants: list[str],
    currency: str,
) -> MandateProposal:
    context = {
        "request": user_text,
        "category": category,
        "attribute_vocabulary": vocabulary,
        "saved_addresses": address_refs,
        "merchants": merchants,
        "default_currency": currency,
    }
    resp = router.call(
        "mandate",
        _request(
            router, "mandate", prompt("mandate_v1"), json.dumps(context, indent=1), "mandate_v1"
        ),
    )
    try:
        raw = parse_json(resp.text)
    except ValueError as e:
        raise RoleError(f"mandate extractor returned invalid JSON: {e}") from e
    errors = list(Draft202012Validator(schema("mandate_v1")).iter_errors(raw))
    if errors:
        raise RoleError(f"mandate extractor output off schema: {errors[0].message}")
    fields = {
        "category": raw["category"] or category,
        "required_attributes": {a["name"]: a["value"] for a in raw["required_attributes"]},
        "forbidden_attributes": {a["name"]: list(a["values"]) for a in raw["forbidden_attributes"]},
        "max_unit_price": raw["max_unit_price"],
        "max_total": raw["max_total"],
        "currency": raw["currency"] or currency,
        "quantity": raw["quantity"],
        "merchant_allowlist": raw["merchant_allowlist"],
        "preference": raw["preference"],
        "ship_to_ref": raw["ship_to_ref"],
    }
    problems = _check_proposal(fields, vocabulary, address_refs, merchants)
    return MandateProposal(fields, list(raw["questions"]), problems)


def _check_proposal(
    f: dict[str, Any], vocab: dict[str, list[str]], refs: list[str], merchants: list[str]
) -> list[str]:
    problems = []
    for name, value in f["required_attributes"].items():
        if value not in vocab.get(name, []):
            problems.append(f"required {name}={value!r} is not in the vocabulary")
    for name, values in f["forbidden_attributes"].items():
        bad = [v for v in values if v not in vocab.get(name, [])]
        if bad:
            problems.append(f"forbidden {name}={bad} not in the vocabulary")
    for key in ("max_unit_price", "max_total", "quantity", "ship_to_ref"):
        if f[key] is None:
            problems.append(f"{key} is missing")
    if f["ship_to_ref"] is not None and f["ship_to_ref"] not in refs:
        problems.append(f"ship_to_ref {f['ship_to_ref']!r} is not a saved address")
    for m in f["merchant_allowlist"] or []:
        if m not in merchants:
            problems.append(f"merchant {m!r} is not registered")
    return problems


# ---- planner -------------------------------------------------------------------------


@dataclass
class PlanResult:
    plan: Plan
    attempts: list[dict[str, Any]] = field(default_factory=list)


def make_plan(
    router: Router,
    mandate_context: dict[str, Any],
    *,
    prompt_name: str = "planner_v2",
    max_repairs: int = 1,
) -> PlanResult:
    """Ask the planner for a plan; if it is invalid, send the validator's error back once."""
    user = json.dumps(mandate_context, indent=1)
    attempts: list[dict[str, Any]] = []
    for attempt in range(1 + max_repairs):
        req = _request(router, "planner", prompt(prompt_name), user, "plan")
        resp = router.call("planner", req, sample_index=attempt)
        try:
            plan = validate_plan(parse_json(resp.text))
        except (ValueError, PlanError) as e:
            attempts.append(
                {
                    "attempt": attempt,
                    "model": f"{resp.provider}/{resp.model}",
                    "error": str(e)[:300],
                }
            )
            user = (
                json.dumps(mandate_context, indent=1)
                + "\n\nYour previous plan was rejected by the validator:\n"
                + str(e)[:500]
                + "\nReturn a corrected plan."
            )
            continue
        attempts.append(
            {
                "attempt": attempt,
                "model": f"{resp.provider}/{resp.model}",
                "plan_hash": plan.plan_hash,
            }
        )
        return PlanResult(plan, attempts)
    raise RoleError(f"planner produced no valid plan in {1 + max_repairs} attempts: {attempts}")


# ---- Q-LLM ---------------------------------------------------------------------------


def extract(router: Router, schema_name: str, text: str, *, seed: int | None = None) -> Any:
    """Schema-valid JSON from untrusted text, or None. The caller labels it UNTRUSTED."""
    system = prompt("extractor_reviews_v1" if schema_name == "reviews_v1" else "extractor_v1")
    resp = router.call(
        "extractor", _request(router, "extractor", system, text, schema_name, seed=seed)
    )
    try:
        data = parse_json(resp.text)
    except ValueError:
        return None
    if list(Draft202012Validator(schema(schema_name)).iter_errors(data)):
        return None
    return data


def rank(
    router: Router,
    candidates: list[dict[str, Any]],
    evidence: Any,
    *,
    preference: str | None = None,
    seed: int | None = None,
    prompt_name: str = "rank_v1",
) -> list[int]:
    if not candidates:
        return []
    user = json.dumps(
        {
            "shopper_preference": preference,
            "candidates": list(enumerate(candidates)),
            "evidence": evidence,
        },
        indent=1,
    )
    resp = router.call(
        "extractor",
        _request(router, "extractor", prompt(prompt_name), user, "rank_v1", seed=seed),
    )
    try:
        data = parse_json(resp.text)
    except ValueError:
        return []
    order = data.get("order") if isinstance(data, dict) else None
    if not isinstance(order, list):
        return []
    return [i for i in order if isinstance(i, int) and not isinstance(i, bool)]
