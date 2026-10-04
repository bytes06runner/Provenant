"""Plan DSL: the small JSON AST the P-LLM planner emits (CLAUDE.md section 4.5).

Not free Python. `validate_plan` checks the JSON schema (`schema.json`) and then static rules the
schema cannot express: each tool call has exactly the arguments its signature allows, every
variable is defined before use, nesting stays shallow, and the plan proposes a checkout.

Statements:  let, if, for_each (with optional collect), propose_checkout
Expressions: var, const, call (tool), select_best
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from lineage.canonical import content_hash

SCHEMA_PATH = Path(__file__).with_name("schema.json")
MAX_DEPTH = 8

# tool -> (required args, optional args)
SIGNATURES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "list_merchants": (frozenset(), frozenset()),
    "fetch_manifest": (frozenset({"merchant"}), frozenset()),
    "search_manifest": (frozenset({"manifest"}), frozenset()),
    "fetch_page": (frozenset({"url"}), frozenset()),
    "extract": (frozenset({"schema", "text"}), frozenset()),
    "rank": (frozenset({"candidates", "evidence"}), frozenset()),
    "precheck": (frozenset({"manifest", "sku"}), frozenset()),
}


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    document: dict[str, Any]
    plan_hash: str

    @property
    def body(self) -> list[dict[str, Any]]:
        return list(self.document["body"])


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_plan(document: Any) -> Plan:
    errors = sorted(_validator().iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "$"
        raise PlanError(f"schema: {where}: {first.message}")
    _check_block(document["body"], defined=set(), depth=0, path="body")
    if not _proposes(document["body"]):
        raise PlanError("plan never proposes a checkout")
    return Plan(document, content_hash(document))


def _check_block(block: list[dict[str, Any]], defined: set[str], depth: int, path: str) -> None:
    if depth > MAX_DEPTH:
        raise PlanError(f"{path}: nesting deeper than {MAX_DEPTH}")
    scope = set(defined)
    for i, stmt in enumerate(block):
        here = f"{path}[{i}]"
        op = stmt["op"]
        if op == "let":
            _check_expr(stmt["expr"], scope, f"{here}.expr")
            scope.add(stmt["name"])
        elif op == "if":
            _check_expr(stmt["cond"], scope, f"{here}.cond")
            _check_block(stmt["then"], scope, depth + 1, f"{here}.then")
            _check_block(stmt.get("else", []), scope, depth + 1, f"{here}.else")
        elif op == "for_each":
            _check_expr(stmt["in"], scope, f"{here}.in")
            inner = scope | {stmt["var"]}
            _check_block(stmt["body"], inner, depth + 1, f"{here}.body")
            collect = stmt.get("collect")
            if collect is not None:
                # The collect expression sees the loop variable and names bound in the body.
                _check_expr(collect["expr"], inner | _bound_names(stmt["body"]), f"{here}.collect")
                scope.add(collect["into"])
        else:  # propose_checkout
            _check_expr(stmt["selection"], scope, f"{here}.selection")


def _bound_names(block: list[dict[str, Any]]) -> set[str]:
    """Names a block binds at its own level: let names and nested loops' collect targets."""
    names = {s["name"] for s in block if s["op"] == "let"}
    names |= {s["collect"]["into"] for s in block if s["op"] == "for_each" and "collect" in s}
    return names


def _check_expr(expr: dict[str, Any], scope: set[str], path: str) -> None:
    if "var" in expr:
        if expr["var"] not in scope:
            raise PlanError(f"{path}: variable {expr['var']!r} used before it is defined")
    elif "call" in expr:
        required, optional = SIGNATURES[expr["call"]]
        given = set(expr["args"])
        missing, extra = required - given, given - required - optional
        if missing or extra:
            tool = expr["call"]
            raise PlanError(
                f"{path}: {tool} args: missing {sorted(missing)}, unexpected {sorted(extra)}"
            )
        for name, arg in expr["args"].items():
            _check_expr(arg, scope, f"{path}.args.{name}")
    elif "select_best" in expr:
        _check_expr(expr["select_best"]["from"], scope, f"{path}.from")
        _check_expr(expr["select_best"]["ranking"], scope, f"{path}.ranking")
    # const needs no checks beyond the schema


def _proposes(block: list[dict[str, Any]]) -> bool:
    for stmt in block:
        op = stmt["op"]
        if op == "propose_checkout":
            return True
        if op == "if" and (_proposes(stmt["then"]) or _proposes(stmt.get("else", []))):
            return True
        if op == "for_each" and _proposes(stmt["body"]):
            return True
    return False
