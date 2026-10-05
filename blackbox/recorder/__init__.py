"""Flight Recorder: an append-only, hash-chained log of every step of every purchase session.

Each event's hash commits to the previous event's hash, so editing, deleting, reordering or
inserting an event breaks verification from that point on (CLAUDE.md section 4.6).

    genesis(session)  = SHA256("provenant/recorder/v1/genesis" || NUL || session_id)
    payload_hash      = SHA256(JCS(payload))
    event_hash        = SHA256(JCS({session_id, seq, event_type, payload_hash, recorded_at,
                                    prev_hash}))

The money is bound to the trace by putting a prefix of the chain head into the PayPal order's
`custom_id` at checkout. Later events extend the chain; `resolve_custom_id` finds the exact event
the order was bound to, and `verify` proves it is part of an intact chain.

Large inputs (page snapshots) are stored once as content-addressed blobs and referenced by hash.
Payloads go through JCS, so floats are refused: money is recorded as decimal strings.
Callers redact secrets before recording (paypal.redact); the recorder stores what it is given.

In-app the recorder only appends. Database-level protection (a Postgres trigger refusing UPDATE
and DELETE on recorder tables) arrives with the Alembic migrations.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from lineage.canonical import canonicalize

GENESIS_DOMAIN = b"provenant/recorder/v1/genesis"

metadata = MetaData()

recorder_events = Table(
    "recorder_events",
    metadata,
    Column("session_id", String(64), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("event_type", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("event_hash", String(64), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("event_hash", name="uq_recorder_event_hash"),
)

recorder_blobs = Table(
    "recorder_blobs",
    metadata,
    Column("content_hash", String(64), primary_key=True),
    Column("media_type", String(128), nullable=False),
    Column("content", LargeBinary, nullable=False),
)


class RecorderError(Exception):
    pass


class ChainBroken(RecorderError):
    def __init__(self, session_id: str, seq: int, reason: str) -> None:
        super().__init__(f"session {session_id}: chain broken at seq {seq}: {reason}")
        self.session_id = session_id
        self.seq = seq
        self.reason = reason


@dataclass(frozen=True)
class Event:
    session_id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    payload_hash: str
    prev_hash: str
    event_hash: str
    recorded_at: datetime


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def genesis(session_id: str) -> str:
    return _sha256(GENESIS_DOMAIN + b"\x00" + session_id.encode())


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _aware(ts: datetime) -> datetime:
    # SQLite drops tzinfo on read; every timestamp we write is UTC.
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def compute_event_hash(
    session_id: str,
    seq: int,
    event_type: str,
    payload_hash: str,
    recorded_at: datetime,
    prev_hash: str,
) -> str:
    header = {
        "session_id": session_id,
        "seq": seq,
        "event_type": event_type,
        "payload_hash": payload_hash,
        "recorded_at": _iso(recorded_at),
        "prev_hash": prev_hash,
    }
    return _sha256(canonicalize(header))


@dataclass(frozen=True)
class CustomIdFormat:
    """From config/app.yaml paypal.custom_id."""

    prefix: str
    hash_hex_chars: int
    max_length: int

    def render(self, event_hash: str) -> str:
        value = self.prefix + event_hash[: self.hash_hex_chars]
        if len(value) > self.max_length:
            raise RecorderError(f"custom_id {len(value)} chars exceeds {self.max_length}")
        return value

    def parse(self, custom_id: str) -> str:
        if not custom_id.startswith(self.prefix):
            raise RecorderError("not a Provenant custom_id")
        prefix = custom_id[len(self.prefix) :]
        if len(prefix) != self.hash_hex_chars or any(c not in "0123456789abcdef" for c in prefix):
            raise RecorderError("malformed custom_id hash prefix")
        return prefix


class FlightRecorder:
    def __init__(self, engine: Engine, *, max_append_attempts: int = 5) -> None:
        self.engine = engine
        self.max_append_attempts = max_append_attempts
        metadata.create_all(engine)  # replaced by Alembic migrations in Phase 1

    # ---- writing -------------------------------------------------------------

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> Event:
        """Append one event at the head of the session's chain.

        Two writers racing on the same session both compute seq = head + 1; the primary key lets
        exactly one win, and the loser retries on top of the new head.
        """
        if not session_id or not event_type:
            raise RecorderError("session_id and event_type are required")
        payload_json = canonicalize(payload)
        payload_hash = _sha256(payload_json)
        for _ in range(self.max_append_attempts):
            head = self.head(session_id)
            seq = 0 if head is None else head.seq + 1
            prev = genesis(session_id) if head is None else head.event_hash
            recorded_at = _aware(now or datetime.now(UTC))
            event_hash = compute_event_hash(
                session_id, seq, event_type, payload_hash, recorded_at, prev
            )
            try:
                with self.engine.begin() as conn:
                    conn.execute(
                        insert(recorder_events).values(
                            session_id=session_id,
                            seq=seq,
                            event_type=event_type,
                            payload_json=payload_json.decode(),
                            payload_hash=payload_hash,
                            prev_hash=prev,
                            event_hash=event_hash,
                            recorded_at=recorded_at,
                        )
                    )
            except IntegrityError:
                continue  # lost the race for this seq; retry on the new head
            return Event(
                session_id, seq, event_type, payload, payload_hash, prev, event_hash, recorded_at
            )
        raise RecorderError(f"could not append to {session_id} after {self.max_append_attempts}")

    def put_blob(self, content: bytes, media_type: str) -> str:
        """Store content once, keyed by its SHA-256. Returns the hash to reference in payloads."""
        content_hash = _sha256(content)
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(recorder_blobs).values(
                        content_hash=content_hash, media_type=media_type, content=content
                    )
                )
        except IntegrityError:
            pass  # already stored; content addressing makes this a no-op
        return content_hash

    # ---- reading -------------------------------------------------------------

    def _row_to_event(self, row: Any) -> Event:
        import json

        return Event(
            session_id=row["session_id"],
            seq=row["seq"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            payload_hash=row["payload_hash"],
            prev_hash=row["prev_hash"],
            event_hash=row["event_hash"],
            recorded_at=_aware(row["recorded_at"]),
        )

    def head(self, session_id: str) -> Event | None:
        q = (
            select(recorder_events)
            .where(recorder_events.c.session_id == session_id)
            .order_by(recorder_events.c.seq.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(q).mappings().first()
        return self._row_to_event(row) if row else None

    def events(self, session_id: str) -> list[Event]:
        """Every event of the session in order: the exact inputs to every step (rehydration)."""
        q = (
            select(recorder_events)
            .where(recorder_events.c.session_id == session_id)
            .order_by(recorder_events.c.seq)
        )
        with self.engine.connect() as conn:
            return [self._row_to_event(r) for r in conn.execute(q).mappings()]

    def latest(self, event_type: str, limit: int = 50) -> list[Event]:
        """The most recent events of one type across all sessions, newest first."""
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(recorder_events)
                    .where(recorder_events.c.event_type == event_type)
                    .order_by(recorder_events.c.recorded_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [self._row_to_event(r) for r in rows]

    def get_blob(self, content_hash: str) -> bytes:
        q = select(recorder_blobs.c.content).where(recorder_blobs.c.content_hash == content_hash)
        with self.engine.connect() as conn:
            content = conn.execute(q).scalar_one_or_none()
        if content is None:
            raise RecorderError(f"no blob {content_hash}")
        if _sha256(content) != content_hash:
            raise RecorderError(f"blob {content_hash} does not match its hash")
        return bytes(content)

    # ---- verification ----------------------------------------------------------

    def verify(self, session_id: str) -> str:
        """Recompute the whole chain. Returns the head hash, or raises ChainBroken."""
        prev = genesis(session_id)
        events = self.events(session_id)
        if not events:
            raise RecorderError(f"no events for session {session_id}")
        for expected_seq, e in enumerate(events):
            if e.seq != expected_seq:
                raise ChainBroken(session_id, expected_seq, f"missing event (found seq {e.seq})")
            if e.prev_hash != prev:
                raise ChainBroken(session_id, e.seq, "prev_hash does not link to prior event")
            if _sha256(canonicalize(e.payload)) != e.payload_hash:
                raise ChainBroken(session_id, e.seq, "payload does not match payload_hash")
            recomputed = compute_event_hash(
                session_id, e.seq, e.event_type, e.payload_hash, e.recorded_at, e.prev_hash
            )
            if recomputed != e.event_hash:
                raise ChainBroken(session_id, e.seq, "event_hash does not match its contents")
            prev = e.event_hash
        return prev

    def resolve_custom_id(self, session_id: str, custom_id: str, fmt: CustomIdFormat) -> Event:
        """The event a PayPal order's custom_id was bound to, after verifying the chain."""
        prefix = fmt.parse(custom_id)
        self.verify(session_id)
        matches = [e for e in self.events(session_id) if e.event_hash.startswith(prefix)]
        if len(matches) != 1:
            raise RecorderError(f"custom_id matches {len(matches)} events in {session_id}")
        return matches[0]
