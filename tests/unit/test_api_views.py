"""Run-view steps and the provenance graph, derived from recorded events."""

from __future__ import annotations

from api.views import parse_source, provenance_graph, short, step

SIGNED_PRICE = "MERCHANT_SIGNED:manifest:kestrel:9c15a8#catalog[KES-001].price"
PAGE = "UNTRUSTED:page:http://shop/m/attacker/reviews/ATT-001"


def test_sources_are_parsed_into_plain_words():
    assert parse_source(SIGNED_PRICE) == {
        "label": "MERCHANT_SIGNED",
        "ref": "manifest:kestrel:9c15a8",
        "path": "catalog[KES-001].price",
        "kind": "manifest",
        "text": "kestrel signed manifest KES-001: price",
    }
    assert parse_source("USER:mandate:m-1#quantity")["text"] == "your signed mandate: quantity"
    assert parse_source("USER:vault:buyer_a:home")["text"] == "your address book: home"
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
