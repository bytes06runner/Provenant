"""Reconciliation poller: PayPal GETs are the source of truth (docs/architecture.md).

Every resource a remedy creates or depends on is tracked here. The poller GETs whatever is due,
records each observed status change once, and stops polling a resource once its status is final.
A verified webhook only calls `hint()`, which makes that resource due now; its body is never
applied to state, so a missing, late, duplicated or forged webhook cannot corrupt anything.

`discrepancies()` compares an order's refunds at PayPal with what our request ledger caused and
reports the ones we did not cause (seen in S6: a FAILED refund PayPal created during a dispute).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from paypal.client import PayPalClient
from paypal.storage import ensure_schema

metadata = MetaData()

tracked = Table(
    "reconcile_tracked",
    metadata,
    Column("resource_id", String(64), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("app", String(64), nullable=False),  # merchant id, or "operator"
    Column("case_id", String(64), nullable=False),
    Column("status", String(32)),
    Column("final", Integer, nullable=False, default=0),
    Column("polls", Integer, nullable=False, default=0),
    Column("next_poll_at", DateTime(timezone=True), nullable=False),
)

# kind -> (GET path template, how to read the status, final statuses)
KINDS: dict[str, tuple[str, Callable[[dict[str, Any]], str], frozenset[str]]] = {
    "refund": (
        "/v2/payments/refunds/{id}",
        lambda b: str(b["status"]),
        frozenset({"COMPLETED", "FAILED", "CANCELLED"}),
    ),
    "payout": (
        "/v1/payments/payouts/{id}",
        lambda b: str(b["batch_header"]["batch_status"]),
        frozenset({"SUCCESS", "DENIED", "CANCELED"}),
    ),
    "authorization": (
        "/v2/payments/authorizations/{id}",
        lambda b: str(b["status"]),
        frozenset({"VOIDED", "CAPTURED", "EXPIRED", "DENIED"}),
    ),
    "capture": (
        "/v2/payments/captures/{id}",
        lambda b: str(b["status"]),
        frozenset({"REFUNDED", "DECLINED", "FAILED"}),
    ),
}


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Observation:
    resource_id: str
    kind: str
    case_id: str
    old: str | None
    new: str
    final: bool


class ReconciliationPoller:
    def __init__(
        self,
        engine: Engine,
        clients: Callable[[str], PayPalClient],
        *,
        record: Callable[[str, str, dict[str, Any]], None],
        interval_seconds: float = 5.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.clients = clients
        self.record = record  # (case_id, event_type, payload)
        self.interval = timedelta(seconds=interval_seconds)
        self.now = now
        ensure_schema(metadata, engine)  # SQLite only; Alembic owns Postgres

    def track(self, kind: str, resource_id: str, app: str, case_id: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown resource kind {kind!r}")
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(tracked).values(
                        resource_id=resource_id,
                        kind=kind,
                        app=app,
                        case_id=case_id,
                        status=None,
                        final=0,
                        polls=0,
                        next_poll_at=self.now(),
                    )
                )
        except IntegrityError:
            self.hint(resource_id)

    def hint(self, resource_id: str) -> None:
        """A webhook said this resource may have changed: poll it now (never trust the body)."""
        with self.engine.begin() as conn:
            conn.execute(
                update(tracked)
                .where(tracked.c.resource_id == resource_id, tracked.c.final == 0)
                .values(next_poll_at=self.now())
            )

    def poll_due(self) -> list[Observation]:
        now = self.now()
        with self.engine.connect() as conn:
            due = conn.execute(select(tracked).where(tracked.c.final == 0)).mappings().all()
        out = []
        for row in due:
            if _aware(row["next_poll_at"]) > now:
                continue
            path, read, finals = KINDS[row["kind"]]
            with self.clients(row["app"]) as client:
                body = client.get(path.format(id=row["resource_id"])).body
            status = read(body)
            final = status in finals
            with self.engine.begin() as conn:
                conn.execute(
                    update(tracked)
                    .where(tracked.c.resource_id == row["resource_id"])
                    .values(
                        status=status,
                        final=int(final),
                        polls=row["polls"] + 1,
                        next_poll_at=now + self.interval,
                    )
                )
            if status != row["status"]:
                obs = Observation(
                    row["resource_id"], row["kind"], row["case_id"], row["status"], status, final
                )
                self.record(
                    row["case_id"],
                    "reconcile.observed",
                    {
                        "kind": obs.kind,
                        "resource_id": obs.resource_id,
                        "from": obs.old,
                        "to": obs.new,
                        "final": obs.final,
                    },
                )
                out.append(obs)
        return out

    def pending(self, case_id: str) -> list[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(tracked.c.resource_id).where(
                    tracked.c.case_id == case_id, tracked.c.final == 0
                )
            ).all()
        return [r[0] for r in rows]

    def settle(self, case_id: str, *, max_rounds: int, sleep: Callable[[float], None]) -> bool:
        """Poll until every resource of the case is final. True if settled."""
        for _ in range(max_rounds):
            self.poll_due()
            if not self.pending(case_id):
                return True
            sleep(self.interval.total_seconds())
        return False

    def discrepancies(
        self, *, case_id: str, order_id: str, app: str, caused: set[str]
    ) -> list[dict[str, Any]]:
        """Refunds on the order that our ledger did not cause."""
        with self.clients(app) as client:
            order = client.get(f"/v2/checkout/orders/{order_id}").body
        refunds = [
            r
            for pu in order.get("purchase_units", [])
            for r in (pu.get("payments") or {}).get("refunds", [])
        ]
        unexplained = [
            {
                "refund_id": r["id"],
                "status": r.get("status"),
                "amount": (r.get("amount") or {}).get("value"),
            }
            for r in refunds
            if r["id"] not in caused
        ]
        if unexplained:
            self.record(
                case_id,
                "reconcile.discrepancy",
                {"order_id": order_id, "unexplained_refunds": unexplained},
            )
        return unexplained
