"""Complaint intake: open a recourse case against a recorded purchase session.

The case is its own Flight Recorder session (`r-<purchase session>`), so the complaint, the
facts, every replay, the attribution and every remedy are hash-chained and auditable. Everything
known about the purchase is read back from the purchase session's events, never re-asked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from blackbox.recorder import Event, FlightRecorder
from lineage.signing import SignedEnvelope


class IntakeError(ValueError):
    pass


@dataclass(frozen=True)
class PurchaseRecord:
    """The facts of a purchase, rehydrated from its recorder session."""

    session_id: str
    mandate_envelope: SignedEnvelope
    merchant_id: str
    sku: str
    signed_attributes: dict[str, str]
    total: str
    order_id: str | None  # None for evaluation purchases that stop before a PayPal order
    custom_id: str | None
    authorization_id: str | None
    capture_id: str | None
    shipment: dict[str, Any] | None
    manifest_envelopes: dict[str, SignedEnvelope]  # merchant_id -> envelope (from blobs)
    manifest_hashes: dict[str, str]  # merchant_id -> hash recorded at purchase time
    pages: dict[str, str]  # url -> content hash of the snapshot
    events: list[Event] = field(repr=False, default_factory=list)


def _one(events: list[Event], event_type: str) -> Event:
    found = [e for e in events if e.event_type == event_type]
    if not found:
        raise IntakeError(f"purchase session has no {event_type!r} event")
    return found[-1]


def _maybe(events: list[Event], event_type: str) -> Event | None:
    found = [e for e in events if e.event_type == event_type]
    return found[-1] if found else None


def load_purchase(
    recorder: FlightRecorder, session_id: str, *, require_order: bool = True
) -> PurchaseRecord:
    recorder.verify(session_id)  # never build a case on a tampered trace
    events = recorder.events(session_id)
    mandate = SignedEnvelope.from_dict(_one(events, "mandate.signed").payload["envelope"])
    proposed = _one(events, "plan.proposed").payload["candidate"]
    created = (
        _one(events, "paypal.order.created")
        if require_order
        else _maybe(events, "paypal.order.created")
    )
    order = created.payload if created else None
    auth = _maybe(events, "paypal.authorization")
    capture = _maybe(events, "paypal.capture")
    shipment = _maybe(events, "fulfillment.shipped")
    envelopes: dict[str, SignedEnvelope] = {}
    hashes: dict[str, str] = {}
    for e in events:
        if e.event_type == "tool.manifest_verified":
            mid = e.payload["merchant_id"]
            hashes[mid] = e.payload["manifest_hash"]
            if e.payload.get("envelope_blob"):
                raw = recorder.get_blob(e.payload["envelope_blob"])
                envelopes[mid] = SignedEnvelope.from_dict(json.loads(raw))
    pages = {
        e.payload["url"]: e.payload["content_hash"]
        for e in events
        if e.event_type == "tool.page_snapshot"
    }
    return PurchaseRecord(
        session_id=session_id,
        mandate_envelope=mandate,
        merchant_id=proposed["merchant_id"],
        sku=proposed["sku"],
        signed_attributes=dict(proposed["attributes"]),
        total=proposed["total"],
        order_id=order["order"]["id"] if order else None,
        custom_id=order["order"]["purchase_units"][0]["custom_id"] if order else None,
        authorization_id=auth.payload["authorization_id"] if auth else None,
        capture_id=capture.payload["capture_id"] if capture else None,
        shipment=dict(shipment.payload) if shipment else None,
        manifest_envelopes=envelopes,
        manifest_hashes=hashes,
        pages=pages,
        events=events,
    )


@dataclass(frozen=True)
class Complaint:
    """What the user reports. `clarified` holds the user's true intent as mandate field edits;
    `reported_attributes` are observations the user confirms (for example waterproof: "no")."""

    text: str
    clarified: dict[str, Any]
    reported_attributes: dict[str, str]
    photo_png: bytes | None = None


def clarify_edits(base: dict[str, Any], edits: dict[str, str]) -> dict[str, Any]:
    """Edits like {"required.waterproof": "yes", "forbidden.material": "leather"} applied to
    the signed mandate's fields: the user's clarified intent, as mandate field edits."""
    out: dict[str, Any] = {}
    for key, value in edits.items():
        section, attr = key.split(".", 1)
        if section == "required":
            req = dict(out.get("required_attributes", base["required_attributes"]))
            req[attr] = value
            out["required_attributes"] = req
        elif section == "forbidden":
            forb = dict(out.get("forbidden_attributes", base["forbidden_attributes"]))
            forb[attr] = sorted({*forb.get(attr, []), value})
            out["forbidden_attributes"] = forb
        else:
            raise ValueError(f"unsupported clarification {key!r}")
    return out


def case_id_for(session_id: str) -> str:
    return f"r-{session_id}"


def file_complaint(recorder: FlightRecorder, purchase: PurchaseRecord, c: Complaint) -> str:
    case = case_id_for(purchase.session_id)
    photo = recorder.put_blob(c.photo_png, "image/png") if c.photo_png else None
    recorder.append(
        case,
        "complaint.filed",
        {
            "purchase_session": purchase.session_id,
            "purchase_chain_head": recorder.verify(purchase.session_id),
            "order_id": purchase.order_id,
            "custom_id": purchase.custom_id,
            "text": c.text,
            "clarified": c.clarified,
            "reported_attributes": c.reported_attributes,
            "photo_blob": photo,
        },
    )
    return case
