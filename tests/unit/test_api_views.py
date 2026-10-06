"""Run-view steps and the provenance graph, derived from recorded events."""

from __future__ import annotations

from api.views import parse_source, provenance_graph, session_from_events, short, step

SIGNED_PRICE = "MERCHANT_SIGNED:manifest:kestrel:9c15a8#catalog[KES-001].price"
PAGE = "UNTRUSTED:page:http://shop/m/attacker/reviews/ATT-001"


def test_sources_are_parsed_into_plain_words():
    assert parse_source(SIGNED_PRICE) == {
        "label": "MERCHANT_SIGNED",
        "ref": "manifest:kestrel:9c15a8",
        "path": "catalog[KES-001].price",
        "kind": "manifest",
        "text": "kestrel signed: KES-001 price",
    }
    assert parse_source("USER:mandate:m-1#quantity")["text"] == "your signed mandate: quantity"
    assert parse_source("USER:vault:buyer_a:home")["text"] == "your address book: home"
    sealed = "MERCHANT_SIGNED:manifest:k:9c"
    assert parse_source(f"{sealed}#paypal_merchant_id")["text"] == "k signed: payee id"
    assert parse_source(f"{sealed}#shipping_flat")["text"] == "k signed: shipping"
    assert parse_source(sealed)["text"] == "k signed: manifest"
    assert parse_source(PAGE)["text"] == "web page http://shop/m/attacker/reviews/ATT-001"
    assert parse_source("UNTRUSTED:qllm:rank")["text"] == "quarantined extractor: rank"
    assert parse_source("DERIVED:registry:merchants")["text"] == "registry:merchants"
    odd = parse_source("weird")
    assert odd["label"] == "UNTRUSTED" and odd["kind"] == "unknown"
    assert short("abcdef", 3) == "abc" and short(None) is None


def test_steps_never_leak_prompts_and_carry_chips():
    s = step(
        "llm.call",
        3,
        "t",
        {
            "role": "planner",
            "cached": True,
            "request": {"messages": ["SECRET PROMPT"]},
            "response": {"provider": "gemini", "model": "flash-lite", "text": "SECRET OUTPUT"},
        },
    )
    assert s["detail"] == "planner: gemini/flash-lite (replayed from cache)"
    assert "SECRET" not in str(s)
    blocked = step(
        "contract.blocked",
        9,
        "t",
        {
            "attack": "payee swap",
            "allowed": False,
            "violations": [{"detail": "untrusted content cannot bind this field"}],
        },
    )
    assert (
        blocked["title"] == "Hijack attempt blocked" and "payee swap blocked" in blocked["detail"]
    )
    assert (
        step("contract.precheck", 1, "t", {"allowed": True, "merchant_id": "n", "sku": "X"})[
            "detail"
        ]
        == "n X passes"
    )
    fetched = step(
        "tool.fetch_manifest", 2, "t", {"label": "MERCHANT_SIGNED", "sources": [SIGNED_PRICE]}
    )
    assert fetched["label"] == "MERCHANT_SIGNED" and "kestrel" in fetched["detail"]
    sealed = step(
        "tool.manifest_verified", 2, "t", {"merchant_id": "k", "manifest_hash": "9c15a82b3d07"}
    )
    assert sealed["detail"] == "k seal 9c15a82b3d"
    signed = step("mandate.signed", 4, "t", {"envelope": {"payload_hash": "a88d5751a7e0ff"}})
    assert signed["label"] == "USER" and signed["detail"] == "signature a88d5751a7"
    proposed = step(
        "plan.proposed",
        5,
        "t",
        {
            "candidate": {"merchant_id": "k", "sku": "K1", "total": "90.52"},
            "decision_label": "UNTRUSTED",
            "decision_sources": [PAGE],
        },
    )
    assert proposed["label"] == "UNTRUSTED" and proposed["sources"][0]["kind"] == "page"
    assert (
        step("plan.select_best", 6, "t", {"index": 0, "options": 2, "ranking_label": "DERIVED"})[
            "detail"
        ]
        == "option 1 of 2"
    )
    assert (
        step("paypal.order.created", 7, "t", {"order": {"id": "O1", "status": "CREATED"}})["detail"]
        == "order O1 (CREATED)"
    )
    assert (
        "A1"
        in step("paypal.authorization", 8, "t", {"authorization_id": "A1", "status": "CREATED"})[
            "detail"
        ]
    )
    assert (
        step("tool.page_snapshot", 8, "t", {"url": "u", "content_hash": "84af923d99ab"})["label"]
        == "UNTRUSTED"
    )
    assert (
        step("tool.fetch_page.refused", 8, "t", {"url": "http://evil"})["detail"] == "http://evil"
    )
    assert step("request", 0, "t", {"text": "shoes"})["label"] == "USER"
    warn = step(
        "llm.budget_warning",
        1,
        "t",
        {"target": "groq/m", "used": 8, "limit": 10, "metric": "tokens_per_day"},
    )
    assert warn["detail"] == "groq/m at 8 of 10 tokens_per_day"
    assert step("something.new", 1, "t", {})["title"] == "something.new"


def field(value, label, *prov):
    return {"value": value, "label": label, "provenance": list(prov)}


def test_provenance_graph_links_fields_to_sources_and_marks_blocked_paths():
    final = {
        "allowed": True,
        "fields": {
            "payee": field(
                "NWPAYEE", "MERCHANT_SIGNED", "MERCHANT_SIGNED:manifest:k:9c#paypal_merchant_id"
            ),
            "unit_price": field("79.00", "MERCHANT_SIGNED", SIGNED_PRICE),
            "amount.total": field("90.52", "DERIVED", SIGNED_PRICE, "USER:mandate:m#quantity"),
            "quantity": field("1", "USER", "USER:mandate:m#quantity"),
        },
    }
    blocked = {
        "attack": "payee from injected page text",
        "order_builder": "refused: contracts blocked this checkout: ['payee']",
        "violations": [{"field": "payee", "rule": "untrusted_in_authority_field", "detail": "x"}],
        "fields": {
            "payee": field("ATK", "UNTRUSTED", PAGE),
            "unit_price": field("79.00", "MERCHANT_SIGNED", SIGNED_PRICE),
        },
    }
    proposed = {
        "decision_label": "UNTRUSTED",
        "decision_sources": [PAGE],
        "candidate": {"sku": "K1"},
    }
    g = provenance_graph(
        [("contract.final", final), ("contract.blocked", blocked), ("plan.proposed", proposed)]
    )
    assert g["allowed"] and [f["name"] for f in g["fields"]] == [
        "Payee",
        "Unit price",
        "Quantity",
        "Total",
    ]
    # the signed price feeds both unit price and total: one source node, two edges
    price = next(s for s in g["sources"] if s["path"] == "catalog[KES-001].price")
    assert sum(e["from"] == price["id"] for e in g["edges"]) == 2
    attempt = g["blocked"][0]
    assert [f["name"] for f in attempt["fields"]] == ["Payee"]  # only the violating field
    blocked["violations"].append(
        {"field": "unit_price", "rule": "manifest_differs_from_payee", "detail": "y"}
    )
    again = provenance_graph([("contract.final", final), ("contract.blocked", blocked)])
    assert [f["name"] for f in again["blocked"][0]["fields"]] == ["Payee"]  # same value: not drawn
    assert attempt["fields"][0]["blocked"] and attempt["order_builder"].startswith("refused")
    bad = [e for e in g["edges"] if e.get("violating")]
    assert len(bad) == 1 and bad[0]["label"] == "UNTRUSTED"
    assert g["decision"]["label"] == "UNTRUSTED"


def test_provenance_graph_of_an_unfinished_session():
    g = provenance_graph([("request", {"text": "x"})])
    assert g == {
        "allowed": False,
        "fields": [],
        "sources": [],
        "edges": [],
        "blocked": [],
        "decision": None,
    }


def test_session_summary_rebuilt_from_recorded_events():
    events = [
        ("mandate.signed", {"envelope": {"payload_hash": "8d473b12a424ffff"}}),
        ("tool.manifest_verified", {"merchant_id": "northwind", "manifest_hash": "13042e8b50ca99"}),
        (
            "plan.proposed",
            {
                "candidate": {"merchant_id": "northwind", "sku": "NOR-001"},
                "decision_label": "UNTRUSTED",
            },
        ),
    ]
    s = session_from_events("s-1", events, "641eb66c")
    assert s["state"] == "proposed" and s["mandate_seal"] == "8d473b12a424" and s["live"] is False
    assert s["manifest_seal"] == "13042e8b50ca" and s["decision_label"] == "UNTRUSTED"
    order = {
        "id": "O1",
        "status": "PAYER_ACTION_REQUIRED",
        "purchase_units": [{"custom_id": "pv:x"}],
        "links": [{"rel": "payer-action", "href": "https://pp/approve"}],
    }
    s = session_from_events(
        "s-1",
        [*events, ("paypal.order.created", {"merchant_id": "northwind", "order": order})],
        "h",
    )
    assert s["state"] == "ordered" and s["order"]["approval_url"] == "https://pp/approve"
    s = session_from_events(
        "s-1", [*events, ("paypal.authorization", {"authorization_id": "A1"})], "h"
    )
    assert s["state"] == "authorized"
    assert session_from_events("s-2", [], None)["state"] == "unknown"


def _ev(*items):
    return [(i, t, p) for i, (t, p) in enumerate(items)]


ATT = {"v": {"observed": "1.0000"}, "shares": {"U": "0.5000", "M": "0.5000", "A": "0.0000"}}
PLAN = {"status": "proposed", "actions": [{"kind": "refund", "amount": "45.26"}]}


def test_case_view_of_a_remedied_case():
    from api.views import case_view

    v = case_view(
        "r-1",
        _ev(
            ("complaint.filed", {"purchase_session": "s-1", "order_id": "O1", "text": "leaks"}),
            ("facts.established", {"misrepresentations": {"waterproof": {}}}),
            ("replay.run", {"coalition": "observed", "bad": 1}),
            ("replay.run", {"coalition": "observed", "bad": 0}),
            ("attribution", ATT),
            ("remedy.proposed", PLAN),
            ("ruling", {"ruling": {"heading": "Ruling"}}),
            ("evidence_pack", {"blob": "abc"}),
            ("remedy.approved", {"by": "operator"}),
            ("remedy.executed", {"kind": "refund", "resource_id": "R1"}),
            ("reconcile.observed", {"resource_id": "R1", "to": "COMPLETED"}),
            ("case.closed", {"settled": True}),
        ),
    )
    assert v["status"] == "remedied" and v["order_id"] == "O1"
    assert v["replays"] == {"observed": {"k": 2, "bad": 1}}
    assert v["attribution"] == ATT and v["evidence_pack"] == "abc" and not v["revised"]
    assert v["executed"][0]["resource_id"] == "R1" and v["closed"]["settled"]


def test_case_view_uses_the_replays_a_revision_was_computed_from():
    from api.views import case_view

    events = _ev(
        ("complaint.filed", {"text": "first run"}),
        ("replay.run", {"coalition": "observed", "bad": 1}),
        ("attribution", ATT),
        ("remedy.proposed", PLAN),
        ("complaint.filed", {"text": "second run, cut short"}),
        ("replay.run", {"coalition": "observed", "bad": 0}),
        ("attribution.revised", {**ATT, "source_complaint_seq": 0}),
        ("remedy.proposed", {"status": "needs_human_review", "actions": []}),
    )
    v = case_view("r-1", events)
    assert v["complaint"]["text"] == "first run" and v["replays"] == {
        "observed": {"k": 1, "bad": 1}
    }
    assert v["revised"] and v["status"] == "human_review"
    legacy = [
        (i, t, {k: x for k, x in p.items() if k != "source_complaint_seq"}) for i, t, p in events
    ]
    assert case_view("r-1", legacy)["complaint"]["text"] == "first run"


def test_case_view_states_without_a_conclusion():
    from api.views import case_view

    assert case_view("r-0", [])["status"] == "unknown"
    running = case_view(
        "r-2",
        _ev(
            ("complaint.filed", {"text": "x"}), ("replay.run", {"coalition": "observed", "bad": 1})
        ),
    )
    assert running["status"] == "in_progress" and running["attribution"] is None
    none = case_view(
        "r-3",
        _ev(
            ("complaint.filed", {}),
            ("attribution", {"note": "fine"}),
            ("remedy.proposed", {"status": "no_remedy"}),
        ),
    )
    assert none["status"] == "no_remedy" and none["attribution_note"] == "fine"


def test_case_view_awaiting_approval():
    from api.views import case_view

    v = case_view(
        "r-4", _ev(("complaint.filed", {}), ("attribution", ATT), ("remedy.proposed", PLAN))
    )
    assert v["status"] == "awaiting_approval" and v["executed"] == []
