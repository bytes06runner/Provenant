"""PayPal webhook intake: signature verification, then idempotent storage.

Order of checks for every delivery:
  1. All PayPal transmission headers present, body is JSON with an event id. Else reject.
  2. Verify with POST /v1/notifications/verify-webhook-signature using the receiving app's
     webhook id. Anything but SUCCESS is rejected and never stored.
  3. Insert keyed on the event id. A second delivery of the same event is acknowledged as a
     duplicate and changes nothing.

Verification comes before dedupe so a forged request reusing a real event id is still rejected.
Applying events to business state (order-independent reducers) is Phase 2; this module only
guarantees that what gets stored is authentic and stored once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from paypal.client import PayPalClient, PayPalError
from paypal.storage import ensure_schema

TRANSMISSION_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}

metadata = MetaData()

webhook_events = Table(
    "webhook_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("app_label", String(64), nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("resource_type", String(64)),
    Column("resource_id", String(64)),
    Column("transmission_id", String(64)),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", Text, nullable=False),
)


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class IntakeResult:
    outcome: Outcome
    http_status: int
    event_id: str | None = None
    event_type: str | None = None
    reason: str | None = None


class WebhookStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        ensure_schema(metadata, engine)  # SQLite only; Alembic owns Postgres

    def add(self, app_label: str, event: dict[str, Any], transmission_id: str | None) -> bool:
        """Store a verified event. Returns False if this event id was already stored."""
        resource = event.get("resource") or {}
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(webhook_events).values(
                        event_id=event["id"],
                        app_label=app_label,
                        event_type=event.get("event_type", ""),
                        resource_type=event.get("resource_type"),
                        resource_id=_resource_id(resource),
                        transmission_id=transmission_id,
                        received_at=datetime.now(UTC),
                        raw_json=json.dumps(event, sort_keys=True),
                    )
                )
        except IntegrityError:
            return False
        return True

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        q = select(webhook_events)
        if event_type:
            q = q.where(webhook_events.c.event_type == event_type)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]


def _resource_id(resource: dict[str, Any]) -> str | None:
    if isinstance(resource.get("batch_header"), dict):
        value = resource["batch_header"].get("payout_batch_id")
    else:
        value = resource.get("id") or resource.get("dispute_id")
    return str(value) if value else None


def verify_signature(
    client: PayPalClient, webhook_id: str, headers: dict[str, str], event: dict[str, Any]
) -> str:
    """Ask PayPal whether this delivery is authentic. Returns 'SUCCESS' or 'FAILURE'."""
    body: dict[str, Any] = {name: headers[h] for name, h in TRANSMISSION_HEADERS.items()}
    body["webhook_id"] = webhook_id
    body["webhook_event"] = event
    resp = client.post("/v1/notifications/verify-webhook-signature", json=body)
    return str(resp.body.get("verification_status", "FAILURE"))


def handle_delivery(
    *,
    app_label: str,
    webhook_id: str,
    headers: dict[str, str],
    raw_body: bytes,
    verifier: PayPalClient,
    store: WebhookStore,
) -> IntakeResult:
    lower = {k.lower(): v for k, v in headers.items()}
    missing = [h for h in TRANSMISSION_HEADERS.values() if not lower.get(h)]
    if missing:
        return IntakeResult(Outcome.REJECTED, 400, reason=f"missing headers: {missing}")
    try:
        event = json.loads(raw_body)
    except ValueError:
        return IntakeResult(Outcome.REJECTED, 400, reason="body is not JSON")
    if not isinstance(event, dict) or not event.get("id"):
        return IntakeResult(Outcome.REJECTED, 400, reason="no event id")

    event_id, event_type = str(event["id"]), event.get("event_type")
    try:
        status = verify_signature(verifier, webhook_id, lower, event)
    except PayPalError as e:
        # Could not verify: tell PayPal to retry later rather than accept unverified data.
        return IntakeResult(Outcome.REJECTED, 503, event_id, event_type, e.summary())
    if status != "SUCCESS":
        return IntakeResult(Outcome.REJECTED, 400, event_id, event_type, f"signature {status}")

    if store.add(app_label, event, lower.get("paypal-transmission-id")):
        return IntakeResult(Outcome.ACCEPTED, 200, event_id, event_type)
    return IntakeResult(Outcome.DUPLICATE, 200, event_id, event_type)
