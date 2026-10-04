"""Request ledger: our own record of every PayPal POST, and the source of truth for idempotency.

Each logical money operation has a caller-chosen `operation_key` (for example
"refund:capture:<capture_id>:remedy:<remedy_id>"). The ledger assigns it exactly one
PayPal-Request-Id and stores the resulting resource id once PayPal answers.

Rules:
  * SUCCEEDED operations are never sent to PayPal again. PayPal's own idempotency window is
    finite, so after it expires a resend could move money twice. The caller reads current
    state with a GET on the recorded resource instead.
  * PENDING (outcome unknown, e.g. crash or timeout) and FAILED operations are retried with
    the same PayPal-Request-Id, so a request that did land at PayPal is not duplicated.
  * Success is "any 2xx". The HTTP status is stored for audit only and never branched on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

paypal_requests = Table(
    "paypal_requests",
    metadata,
    Column("operation_key", String(255), primary_key=True),
    Column("request_id", String(64), nullable=False, unique=True),
    Column("app_label", String(64), nullable=False),
    Column("method", String(8), nullable=False),
    Column("path", String(512), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("http_status", Integer),
    Column("resource_id", String(64)),
    Column("resource_status", String(64)),
    Column("debug_id", String(64)),
    Column("error", String(512)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class OpState(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class LedgerEntry:
    operation_key: str
    request_id: str
    app_label: str
    method: str
    path: str
    state: OpState
    attempts: int
    http_status: int | None
    resource_id: str | None
    resource_status: str | None
    debug_id: str | None
    error: str | None

    @classmethod
    def from_row(cls, row: RowMapping) -> LedgerEntry:
        return cls(
            operation_key=row["operation_key"],
            request_id=row["request_id"],
            app_label=row["app_label"],
            method=row["method"],
            path=row["path"],
            state=OpState(row["state"]),
            attempts=row["attempts"],
            http_status=row["http_status"],
            resource_id=row["resource_id"],
            resource_status=row["resource_status"],
            debug_id=row["debug_id"],
            error=row["error"],
        )


class LedgerConflict(RuntimeError):
    """An operation key was reused for a different app, method or path."""


def _now() -> datetime:
    return datetime.now(UTC)


class RequestLedger:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata.create_all(engine)  # replaced by Alembic migrations in Phase 1

    @classmethod
    def from_url(cls, url: str) -> RequestLedger:
        return cls(create_engine(url))

    def get(self, operation_key: str) -> LedgerEntry | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(paypal_requests).where(paypal_requests.c.operation_key == operation_key)
                )
                .mappings()
                .first()
            )
        return LedgerEntry.from_row(row) if row else None

    def begin(self, operation_key: str, app_label: str, method: str, path: str) -> LedgerEntry:
        """Return the operation's entry, creating it (PENDING, new request id) if new.

        Persisted before the request is sent, so a crash mid-call leaves a PENDING entry whose
        request id is reused on the next attempt.
        """
        now = _now()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(paypal_requests).values(
                        operation_key=operation_key,
                        request_id=str(uuid.uuid4()),
                        app_label=app_label,
                        method=method,
                        path=path,
                        state=OpState.PENDING.value,
                        attempts=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError:
            pass  # already exists: fall through and return it
        entry = self.get(operation_key)
        assert entry is not None
        if (entry.app_label, entry.method, entry.path) != (app_label, method, path):
            raise LedgerConflict(
                f"operation {operation_key!r} was first used for {entry.app_label} "
                f"{entry.method} {entry.path}, not {app_label} {method} {path}"
            )
        return entry

    def mark_attempt(self, operation_key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(paypal_requests)
                .where(paypal_requests.c.operation_key == operation_key)
                .values(attempts=paypal_requests.c.attempts + 1, updated_at=_now())
            )

    def succeed(
        self,
        operation_key: str,
        *,
        http_status: int,
        resource_id: str | None,
        resource_status: str | None,
        debug_id: str | None,
    ) -> LedgerEntry:
        with self.engine.begin() as conn:
            conn.execute(
                update(paypal_requests)
                .where(paypal_requests.c.operation_key == operation_key)
                .values(
                    state=OpState.SUCCEEDED.value,
                    http_status=http_status,
                    resource_id=resource_id,
                    resource_status=resource_status,
                    debug_id=debug_id,
                    error=None,
                    updated_at=_now(),
                )
            )
        entry = self.get(operation_key)
        assert entry is not None
        return entry

    def fail(
        self, operation_key: str, *, http_status: int | None, error: str, debug_id: str | None
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(paypal_requests)
                .where(paypal_requests.c.operation_key == operation_key)
                .values(
                    state=OpState.FAILED.value,
                    http_status=http_status,
                    debug_id=debug_id,
                    error=error[:512],
                    updated_at=_now(),
                )
            )
