"""What the buyer app shows, derived from Flight Recorder events. Pure functions, no I/O.

`step` turns one recorded event into a run-view line (title, detail, label chip, sources).
`provenance_graph` builds the graph of the final order's fields and every source behind them,
plus any blocked hijack attempts with their violating paths.
"""

from __future__ import annotations

from typing import Any

FIELD_ORDER = [
    "payee",
    "item.sku",
    "unit_price",
    "quantity",
    "amount.total",
    "currency",
    "shipping_address",
]
FIELD_NAMES = {
    "payee": "Payee",
    "item.sku": "Item",
    "unit_price": "Unit price",
    "quantity": "Quantity",
    "amount.total": "Total",
    "currency": "Currency",
    "shipping_address": "Ship to",
}
LABELS = ("USER", "MERCHANT_SIGNED", "DERIVED", "UNTRUSTED")


def short(h: str | None, n: int = 10) -> str | None:
    return h[:n] if h else h


def parse_source(s: str) -> dict[str, str]:
    """'MERCHANT_SIGNED:manifest:kestrel:9c15a8...#catalog[KES-001].sku' -> parts."""
    label, _, rest = s.partition(":")
    ref, _, path = rest.partition("#")
    kind = ref.split(":", 1)[0]
    if label not in LABELS:
        label, ref, kind = "UNTRUSTED", s, "unknown"
    return {"label": label, "ref": ref, "path": path, "kind": kind, "text": _source_text(ref, path)}


_MANIFEST_WORDS = {
    "paypal_merchant_id": "payee id",
    "sku": "item",
    "price": "price",
    "shipping_flat": "shipping",
    "tax_rate": "tax rate",
    "currency": "currency",
}


def _source_text(ref: str, path: str) -> str:
    parts = ref.split(":")
    kind = parts[0]
    if kind == "manifest" and len(parts) >= 3:
        what = path.split(".")[-1] if path else "manifest"
        what = _MANIFEST_WORDS.get(what, what.replace("_", " "))
        item = path.split("[")[1].split("]")[0] if "[" in path else ""
        return f"{parts[1]} signed: {item + ' ' if item else ''}{what}"
    if kind == "mandate":
        return f"your signed mandate: {path or 'mandate'}"
    if kind == "vault":
        return f"your address book: {parts[-1]}"
    if kind == "page":
        return f"web page {':'.join(parts[1:])}"
    if kind == "qllm":
        return f"quarantined extractor: {':'.join(parts[1:])}"
    return f"{ref}{' ' + path if path else ''}"


def _chip(payload: dict[str, Any]) -> str | None:
    label = payload.get("label")
    return label if label in LABELS else None


_TITLES = {
    "request": "Request received",
    "agent.policy": "Agent policy",
    "mandate.proposed": "Mandate drafted from your words",
    "mandate.signed": "Mandate confirmed and signed",
    "plan.generated": "Plan written by the planner",
    "plan.started": "Plan running",
    "tool.list_merchants": "Listed registered merchants",
    "tool.fetch_manifest": "Fetched a merchant manifest",
    "tool.manifest_verified": "Manifest signature verified",
    "tool.fetch_manifest.rejected": "Manifest rejected",
    "tool.search_manifest": "Searched the catalog",
    "tool.precheck": "Contract precheck",
    "contract.precheck": "Contract precheck",
    "tool.fetch_page": "Read a web page",
    "tool.page_snapshot": "Page snapshot stored",
    "tool.fetch_page.refused": "Page refused (not a registered merchant)",
    "tool.extract": "Quarantined extraction",
    "tool.rank": "Ranked the candidates",
    "plan.select_best": "Selected a candidate",
    "plan.proposed": "Checkout proposed",
    "contract.final": "Final contract check",
    "contract.blocked": "Hijack attempt blocked",
    "contract.allowed": "Probe allowed",
    "checkout.order_request": "PayPal order request built",
    "paypal.order.created": "PayPal order created",
    "paypal.authorization": "Payment authorized",
    "llm.call": "Model call",
    "llm.budget_warning": "Model budget warning",
    "llm.fallback": "Model fallback",
}


def step(event_type: str, seq: int, at: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One run-view line. Never includes prompts, page text or secrets."""
    detail = ""
    chip = _chip(payload)
    sources = [parse_source(s) for s in payload.get("sources", [])][:6]
    if event_type == "llm.call":
        resp = payload.get("response") or {}
        detail = f"{payload.get('role')}: {resp.get('provider')}/{resp.get('model')}"
        if payload.get("cached"):
            detail += " (replayed from cache)"
    elif event_type in ("contract.precheck", "contract.final", "contract.blocked"):
        allowed = payload.get("allowed")
        who = payload.get("attack") or payload.get("merchant_id") or ""
        sku = f" {payload['sku']}" if payload.get("sku") else ""
        verdict = "passes" if allowed else "blocked"
        reasons = "; ".join(v["detail"] for v in payload.get("violations", []))
        detail = f"{who}{sku} {verdict}{': ' + reasons if reasons else ''}".strip()
        chip = None
    elif event_type == "tool.manifest_verified":
        detail = f"{payload.get('merchant_id')} seal {short(payload.get('manifest_hash'))}"
        chip = "MERCHANT_SIGNED"
    elif event_type == "mandate.signed":
        env = payload.get("envelope", {})
        detail = f"signature {short(env.get('payload_hash') or env.get('signature'))}"
        chip = "USER"
    elif event_type == "plan.proposed":
        c = payload.get("candidate", {})
        detail = f"{c.get('merchant_id')} {c.get('sku')} total {c.get('total')}"
        chip = payload.get("decision_label")
        sources = [parse_source(s) for s in payload.get("decision_sources", [])][:6]
    elif event_type == "plan.select_best":
        detail = f"option {payload.get('index', 0) + 1} of {payload.get('options')}"
        chip = payload.get("ranking_label")
    elif event_type == "paypal.order.created":
        order = payload.get("order", {})
        detail = f"order {order.get('id')} ({order.get('status')})"
    elif event_type == "paypal.authorization":
        detail = f"authorization {payload.get('authorization_id')} ({payload.get('status')})"
    elif event_type == "tool.page_snapshot":
        detail = f"{payload.get('url')} ({short(payload.get('content_hash'))})"
        chip = "UNTRUSTED"
    elif event_type == "tool.fetch_page.refused":
        detail = str(payload.get("url"))
    elif event_type == "request":
        detail = str(payload.get("text", ""))[:200]
        chip = "USER"
    if not detail and sources:
        detail = "; ".join(dict.fromkeys(x["text"] for x in sources))[:200]
    if not detail and event_type == "llm.budget_warning":
        p = payload
        detail = f"{p.get('target')} at {p.get('used')} of {p.get('limit')} {p.get('metric')}"
    return {
        "seq": seq,
        "at": at,
        "type": event_type,
        "title": _TITLES.get(event_type, event_type),
        "detail": detail,
        "label": chip,
        "sources": sources,
    }


def provenance_graph(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Fields of the final order (right), their sources (left), edges colored by label."""
    final = next((p for t, p in reversed(events) if t == "contract.final"), None)
    proposed = next((p for t, p in reversed(events) if t == "plan.proposed"), None)
    blocked = [p for t, p in events if t == "contract.blocked"]
    sources: dict[str, dict[str, Any]] = {}
    fields: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def source_id(s: str) -> str:
        if s not in sources:
            sources[s] = {"id": f"s{len(sources)}", **parse_source(s)}
        return str(sources[s]["id"])

    if final and final.get("fields"):
        for name in FIELD_ORDER:
            f = final["fields"].get(name)
            if f is None:
                continue
            fid = f"f:{name}"
            fields.append(
                {
                    "id": fid,
                    "field": name,
                    "name": FIELD_NAMES[name],
                    "value": f["value"],
                    "label": f["label"],
                    "blocked": False,
                }
            )
            for s in f["provenance"]:
                sid = source_id(s)
                edges.append({"from": sid, "to": fid, "label": sources[s]["label"]})
    attempts = []
    for i, b in enumerate(blocked):
        bad = {v["field"] for v in b.get("violations", [])}
        attempt_fields = []
        for name in FIELD_ORDER:
            f = (b.get("fields") or {}).get(name)
            if f is None or name not in bad:
                continue
            fid = f"b{i}:{name}"
            attempt_fields.append(
                {
                    "id": fid,
                    "field": name,
                    "name": FIELD_NAMES[name],
                    "value": f["value"],
                    "label": f["label"],
                    "blocked": True,
                }
            )
            for s in f["provenance"]:
                sid = source_id(s)
                edges.append(
                    {"from": sid, "to": fid, "label": sources[s]["label"], "violating": True}
                )
        attempts.append(
            {
                "attack": b.get("attack"),
                "order_builder": b.get("order_builder"),
                "violations": b.get("violations", []),
                "fields": attempt_fields,
            }
        )
    decision = None
    if proposed:
        decision = {
            "label": proposed.get("decision_label"),
            "sources": [parse_source(s) for s in proposed.get("decision_sources", [])],
            "candidate": proposed.get("candidate"),
        }
    return {
        "allowed": bool(final and final.get("allowed")),
        "fields": fields,
        "sources": list(sources.values()),
        "edges": edges,
        "blocked": attempts,
        "decision": decision,
    }


def session_from_events(
    sid: str, events: list[tuple[str, dict[str, Any]]], head: str | None
) -> dict[str, Any]:
    """A session's summary rebuilt from its recorded events (after an API restart)."""
    out: dict[str, Any] = {
        "id": sid,
        "chain_head": head,
        "state": "archived" if events else "unknown",
        "live": False,  # read-only: actions need the live session
    }

    def last(t: str) -> dict[str, Any] | None:
        return next((p for et, p in reversed(events) if et == t), None)

    signed = last("mandate.signed")
    if signed:
        out["mandate_seal"] = short(signed["envelope"]["payload_hash"], 12)
    proposed = last("plan.proposed")
    if proposed:
        out["chosen"] = proposed.get("candidate")
        out["decision_label"] = proposed.get("decision_label")
        out["state"] = "proposed"
    verified = [p for et, p in events if et == "tool.manifest_verified"]
    if proposed and verified:
        mid = proposed["candidate"]["merchant_id"]
        seal = next((v["manifest_hash"] for v in verified if v.get("merchant_id") == mid), None)
        out["manifest_seal"] = short(seal, 12)
    created = last("paypal.order.created")
    if created:
        order = created["order"]
        link = next(
            (
                x["href"]
                for x in order.get("links", [])
                if x.get("rel") in ("payer-action", "approve")
            ),
            None,
        )
        out["order"] = {
            "id": order["id"],
            "merchant_id": created["merchant_id"],
            "status": order.get("status"),
            "custom_id": order["purchase_units"][0].get("custom_id"),
            "approval_url": link,
        }
        out["state"] = "ordered"
    if last("paypal.authorization"):
        out["state"] = "authorized"
    return out
