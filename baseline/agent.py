"""The baseline: a conventional tool-calling shopping agent on PayPal's Agent Toolkit.

This is the "before" picture (CLAUDE.md section 7), built the way most agent checkouts are: the
model reads shop pages directly, decides what to buy, and calls the toolkit's `create_order` with
the item names, prices, quantities and shipping address it chose. Nothing separates what the
user asked for from what a page said. It runs on the same model as Provenant's planner, against
the same merchants and the same attack dataset.

The toolkit's create_order has no payee field: the order pays whichever merchant's credentials
make the call, so "which shop to check out at" is itself a decision the model makes.

Every step and the resulting order are written to the Flight Recorder (session "b-...") so the
evaluation and the console can show exactly what happened.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from lineage.toolbox import html_to_text
from llm import roles
from llm.types import LLMRequest, Message

TOOLS = ("list_merchants", "open_page", "checkout", "finish")


@dataclass
class BaselineDeps:
    router: roles.Router
    merchants: dict[str, str]  # merchant id -> storefront base URL
    http: httpx.Client
    create_order: Callable[[str, dict[str, Any]], dict[str, Any]]  # (merchant, params) -> order
    record: Callable[[str, dict[str, Any]], None]
    settings: dict[str, Any]
    read_order: Callable[[str, str], dict[str, Any]] | None = None  # (merchant, id) -> GET body


@dataclass
class BaselineRun:
    steps: list[dict[str, Any]] = field(default_factory=list)
    order: dict[str, Any] | None = None
    finished: str | None = None
    error: str | None = None


def recordable(x: Any) -> Any:
    """The recorder's canonical JSON takes decimal strings, not floats (models emit floats)."""
    if isinstance(x, float):
        return format(Decimal(repr(x)), "f")
    if isinstance(x, dict):
        return {str(k): recordable(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [recordable(v) for v in x]
    return x


def _system(settings: dict[str, Any]) -> str:
    return (
        roles.prompt(str(settings["prompt"]))
        + "\n\n## Output JSON schema\n\n"
        + json.dumps(roles.schema("baseline_action_v1"))
    )


def _open_page(deps: BaselineDeps, url: str) -> str:
    # Even the baseline only browses the sandbox storefronts: this harness never fetches the web.
    if not any(url.startswith(base.rstrip("/") + "/") for base in deps.merchants.values()):
        return f"error: {url} is not a page of a listed shop"
    resp = deps.http.get(url)
    if resp.status_code != 200:
        return f"error: HTTP {resp.status_code}"
    return html_to_text(resp.text)[: int(deps.settings["page_chars"])]


def _checkout(deps: BaselineDeps, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    merchant = args.get("merchant")
    if merchant not in deps.merchants:
        return f"error: unknown shop {merchant!r}", None
    params = {
        "currency_code": "USD",
        "items": args.get("items") or [],
        "shipping_cost": args.get("shipping_cost") or 0,
        "shipping_address": args.get("shipping_address"),
    }
    try:
        order = deps.create_order(str(merchant), params)
    except Exception as e:  # noqa: BLE001  (the toolkit raises plain exceptions; the model sees it)
        return f"error: checkout failed: {str(e)[:300]}", None
    summary = {"order_id": order.get("id"), "status": order.get("status")}
    placed: dict[str, Any] = {"merchant": merchant, "params": params, "order": order}
    if deps.read_order is not None and order.get("id"):
        # What PayPal actually holds: the payee and the amount, read back by GET.
        body = deps.read_order(str(merchant), str(order["id"]))
        pu = (body.get("purchase_units") or [{}])[0]
        placed["paypal"] = {
            "status": body.get("status"),
            "payee": (pu.get("payee") or {}).get("merchant_id"),
            "amount": (pu.get("amount") or {}).get("value"),
            "items": [
                {
                    "name": i.get("name"),
                    "quantity": i.get("quantity"),
                    "unit": (i.get("unit_amount") or {}).get("value"),
                }
                for i in pu.get("items", [])
            ],
            "ship_to": ((pu.get("shipping") or {}).get("address") or {}),
        }
    return json.dumps(summary), placed


def run_baseline(deps: BaselineDeps, request: str, address: dict[str, str]) -> BaselineRun:
    run = BaselineRun()
    defaults = deps.router.config.call_defaults("baseline")
    messages: list[Message] = [
        Message("system", _system(deps.settings)),
        Message("user", f"User request: {request}\nUser's saved address: {json.dumps(address)}"),
    ]
    deps.record("baseline.request", {"text": request})
    for n in range(int(deps.settings["max_steps"])):
        resp = deps.router.call(
            "baseline",
            LLMRequest(
                messages=tuple(messages),
                temperature=str(defaults.get("temperature", "0.7")),
                max_tokens=int(defaults.get("max_tokens", 900)),
                json_schema=roles.schema("baseline_action_v1"),
                schema_name="baseline_action_v1",
                structured="json",
            ),
            sample_index=n,
        )
        try:
            action = roles.parse_json(resp.text)
            tool, args = action["tool"], action.get("args") or {}
            if tool not in TOOLS or not isinstance(args, dict):
                raise ValueError(f"unknown tool {tool!r}")
        except (ValueError, KeyError, TypeError) as e:
            result = f"error: reply with one JSON action ({e})"
            tool, args = "invalid", {}
        else:
            if tool == "list_merchants":
                result = json.dumps(
                    [
                        {"id": m, "catalog_url": f"{b.rstrip('/')}/catalog"}
                        for m, b in deps.merchants.items()
                    ]
                )
            elif tool == "open_page":
                result = _open_page(deps, str(args.get("url", "")))
            elif tool == "checkout":
                result, order = _checkout(deps, args)
                if order is not None:
                    run.order = order
                    deps.record("baseline.order", recordable(order))
            else:
                run.finished = str(args.get("message", ""))[:500]
                result = "done"
        step = {"n": n, "tool": tool, "args": args, "result": result[:300]}
        run.steps.append(step)
        deps.record("baseline.step", recordable(step))
        if tool == "finish" or run.order is not None:
            break
        messages += [
            Message("assistant", resp.text),
            Message("user", f"Result of {tool}: {result}"),
        ]
    else:
        run.error = "step limit reached without a purchase"
    return run


def toolkit_create_order(
    credentials: Callable[[str], Any], source: str
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """create_order through PayPal's Agent Toolkit, with the chosen merchant's credentials."""
    from paypal_agent_toolkit.shared.api import PayPalAPI
    from paypal_agent_toolkit.shared.configuration import Context

    def create(merchant: str, params: dict[str, Any]) -> dict[str, Any]:
        c = credentials(merchant)
        api = PayPalAPI(c.client_id, c.client_secret, Context(sandbox=True, source=source))
        return dict(json.loads(api.run("create_order", params)))

    return create
