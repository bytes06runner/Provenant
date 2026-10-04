"""S6: dispute lifecycle on a buyer-filed sandbox dispute, one step at a time.

Steps (run in order, inspecting the printed state and allowed actions between them):
  show        GET the dispute: status, stage, allowed actions, disputed transaction
  message     seller sends a message to the buyer
  require-evidence  sandbox-only: move an UNDER_REVIEW dispute back to waiting for seller evidence
  evidence    seller uploads an evidence pack PDF (multipart provide-evidence)
  escalate    seller escalates the inquiry to a PayPal claim
  adjudicate  sandbox-only settle (paypal/sandbox_only.py), --outcome BUYER_FAVOR|SELLER_FAVOR

Every step GETs the dispute afterwards and lists CUSTOMER.DISPUTE.* webhook deliveries stored by
spikes/webhook_listener.py, which should be running.

Run: python spikes/s6_disputes.py <step> --dispute-id <id> [--outcome ...]
"""

from __future__ import annotations

import json
from typing import Any

from _common import (
    PayPalClient,
    PayPalError,
    Spike,
    make_parser,
    merchant_client,
    repo_sqlite_url,
    spike_config,
)
from sqlalchemy import create_engine

from paypal.sandbox_only import AdjudicationOutcome, adjudicate, require_evidence
from paypal.webhooks import WebhookStore

STEPS = ("show", "message", "require-evidence", "evidence", "escalate", "adjudicate")
# HATEOAS rel the dispute must offer before a step may call PayPal. The sandbox docs require the
# adjudicate and require-evidence links to be present, and calling early returns
# ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE (seen in S6).
REQUIRED_LINK = {
    "message": "send_message",
    "require-evidence": "require_evidence",
    "evidence": "provide_evidence",
    "escalate": "escalate",
    "adjudicate": "adjudicate",
}


def summarize(d: dict[str, Any]) -> dict[str, Any]:
    txn = (d.get("disputed_transactions") or [{}])[0]
    return {
        "status": d.get("status"),
        "stage": d.get("dispute_life_cycle_stage"),
        "state": d.get("dispute_state"),
        "reason": d.get("reason"),
        "amount": d.get("dispute_amount"),
        "outcome": (d.get("dispute_outcome") or d.get("outcome") or {}),
        "seller_transaction_id": txn.get("seller_transaction_id"),
        "custom": txn.get("custom"),
        "invoice_number": txn.get("invoice_number"),
        "actions": sorted(link["rel"] for link in d.get("links", []) if link["rel"] != "self"),
        "evidences": len(d.get("evidences", [])),
        "messages": len(d.get("messages", [])),
    }


def snapshot(client: PayPalClient, spike: Spike, dispute_id: str, label: str) -> dict[str, Any]:
    d = client.get(f"/v1/customer/disputes/{dispute_id}").body
    s = summarize(d)
    spike.log.append({"step": f"dispute state {label}", "summary": s})
    print(f"\n{label}: {json.dumps(s, indent=1)}")
    return d


def minimal_pdf(lines: list[str]) -> bytes:
    """A valid one-page PDF with plain text. Stand-in for the Phase 2 evidence pack builder."""

    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({esc(t)}) '" for t in lines) + " ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(text) + text.encode("latin-1") + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def run_step(client: PayPalClient, spike: Spike, step: str, dispute_id: str, outcome: str) -> None:
    base = f"/v1/customer/disputes/{dispute_id}"
    before = snapshot(client, spike, dispute_id, "before")
    s = summarize(before)
    # Stage is part of the operation: evidence in INQUIRY and again in CHARGEBACK are distinct.
    key = f"s6:{dispute_id}:{s['stage']}:{step}"
    rel = REQUIRED_LINK.get(step)
    if rel is not None and rel not in s["actions"]:
        spike.check(f"{step}: dispute offers the {rel!r} link", False, f"offered: {s['actions']}")
        return

    if step == "message":
        r = client.post(
            f"{base}/send-message",
            json={
                "message": "Provenant spike S6: we are reviewing this order and will update you."
            },
            operation_key=key,
        )
        spike.step("send-message", r)
    elif step == "require-evidence":
        r = require_evidence(client, dispute_id, "SELLER_EVIDENCE", operation_key=key)
        spike.step("require-evidence SELLER_EVIDENCE (sandbox only)", r)
    elif step == "evidence":
        lines = [
            "Provenant evidence pack (Phase 0 spike S6)",
            f"Dispute: {dispute_id}  Reason: {s['reason']}",
            f"Capture: {s['seller_transaction_id']}  Invoice: {s['invoice_number']}",
            f"Decision trace binding (custom_id): {s['custom']}",
            "This pack will carry the signed mandate, signed manifest, signature proofs,",
            "Flight Recorder excerpts and the attribution report once Phase 2 lands.",
        ]
        pdf = minimal_pdf(lines)
        # Use the type PayPal asked the seller for (evidences[].source == REQUESTED_FROM_SELLER);
        # "OTHER" was refused with EVIDENCE_TYPE_IS_NOT_ALLOWED on an INR inquiry.
        requested = [
            e["evidence_type"]
            for e in before.get("evidences", [])
            if e.get("source") == "REQUESTED_FROM_SELLER"
        ]
        evidence_type = requested[-1] if requested else "OTHER"
        evidence_input = {
            "evidences": [
                {
                    "evidence_type": evidence_type,
                    "notes": (
                        "Provenant evidence pack: decision trace and order binding. "
                        "Sandbox spike item, not shipped by a carrier; no tracking number."
                    ),
                    "documents": [{"name": "provenant-evidence-pack.pdf"}],
                }
            ]
        }
        r = client.post(
            f"{base}/provide-evidence",
            files={
                "input": (None, json.dumps(evidence_input), "application/json"),
                "file1": ("provenant-evidence-pack.pdf", pdf, "application/pdf"),
            },
            operation_key=key,
        )
        spike.step(f"provide-evidence {evidence_type} ({len(pdf)} byte PDF)", r)
    elif step == "escalate":
        r = client.post(
            f"{base}/escalate",
            json={"note": "Provenant spike S6: escalating for PayPal review with evidence."},
            operation_key=key,
        )
        spike.step("escalate", r)
    elif step == "adjudicate":
        r = adjudicate(client, dispute_id, AdjudicationOutcome(outcome), operation_key=key)
        spike.step(f"adjudicate {outcome} (sandbox only)", r)

    if step != "show":
        spike.check(f"{step} accepted (2xx)", 200 <= r.status_code < 300, str(r.status_code))
        after = snapshot(client, spike, dispute_id, "after")
        spike.check(
            f"{step}: state read back with GET",
            bool(after.get("status")),
            f"{s['status']} -> {after.get('status')}",
        )


def main() -> int:
    parser = make_parser(__doc__ or "")
    parser.add_argument("step", choices=STEPS)
    parser.add_argument("--dispute-id", required=True)
    parser.add_argument("--outcome", choices=[o.value for o in AdjudicationOutcome])
    args = parser.parse_args()
    if args.step == "adjudicate" and not args.outcome:
        parser.error("adjudicate needs --outcome")
    cfg = spike_config()
    spike = Spike(f"S6-{args.step}")
    client = merchant_client(args.merchant or cfg["merchant"])
    try:
        with client:
            run_step(client, spike, args.step, args.dispute_id, args.outcome or "")
    except PayPalError as e:
        spike.error(args.step, e)
        spike.check("no PayPal error", False, e.summary())
    except Exception as e:
        spike.crashed(e)

    store = WebhookStore(create_engine(repo_sqlite_url(cfg["s5"]["store_url"])))
    dispute_events = [
        (e["received_at"].isoformat(), e["event_type"], e["event_id"])
        for e in store.events()
        if e["event_type"].startswith("CUSTOMER.DISPUTE") and e["resource_id"] == args.dispute_id
    ]
    spike.log.append({"step": "dispute webhooks stored so far", "events": dispute_events})
    print(f"\nCUSTOMER.DISPUTE.* webhooks stored for {args.dispute_id}: {len(dispute_events)}")
    for row in dispute_events:
        print("  ", *row)
    if args.step == "show":
        spike.check("dispute readable", True)
    return spike.finish()


if __name__ == "__main__":
    raise SystemExit(main())
