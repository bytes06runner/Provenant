"""Thin PayPal REST client.

Responsibilities, and nothing more:
  * OAuth client-credentials token per REST app, cached until shortly before expiry
  * PayPal-Request-Id idempotency key on every POST (caller may pin one to retry safely)
  * `Prefer: return=representation` on every POST, so responses carry the full resource
  * Optional RequestLedger: every POST is persisted with its request id and resulting resource
    id, and an operation the ledger already marks SUCCEEDED is never sent again
  * Retries with exponential backoff on 5xx and transport errors only
  * Structured errors carrying PayPal's name / issue codes and debug_id

Success means any 2xx. Callers never branch on 200 vs 201; they read state from the resource
(and verify it with a GET after every state change).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from paypal.config import Credentials, HttpSettings
from paypal.ledger import LedgerEntry, OpState, RequestLedger


@dataclass
class PayPalError(Exception):
    status_code: int
    method: str
    path: str
    name: str | None = None
    message: str | None = None
    debug_id: str | None = None
    issues: list[str] = field(default_factory=list)
    body: Any = None

    def __str__(self) -> str:
        issues = ",".join(self.issues) or "-"
        return (
            f"PayPal {self.method} {self.path} -> {self.status_code} "
            f"name={self.name} issues={issues} debug_id={self.debug_id}: {self.message}"
        )

    @classmethod
    def from_response(cls, resp: httpx.Response) -> PayPalError:
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        name = message = debug_id = None
        issues: list[str] = []
        if isinstance(body, dict):
            name = body.get("name") or body.get("error")
            message = body.get("message") or body.get("error_description")
            debug_id = body.get("debug_id")
            issues = [d.get("issue", "") for d in body.get("details", []) if isinstance(d, dict)]
        return cls(
            status_code=resp.status_code,
            method=resp.request.method,
            path=resp.request.url.path,
            name=name,
            message=message,
            debug_id=debug_id or resp.headers.get("paypal-debug-id"),
            issues=issues,
            body=body,
        )


class AlreadyCompleted(Exception):
    """The ledger says this operation already succeeded; nothing was sent to PayPal.

    Read current state with a GET on `entry.resource_id`.
    """

    def __init__(self, entry: LedgerEntry) -> None:
        super().__init__(
            f"operation {entry.operation_key!r} already succeeded: resource {entry.resource_id}"
        )
        self.entry = entry


def resource_ref(body: Any) -> tuple[str | None, str | None]:
    """(id, status) of the resource a POST created or changed, across Orders/Payments/Payouts."""
    if not isinstance(body, dict):
        return None, None
    if isinstance(body.get("batch_header"), dict):
        header = body["batch_header"]
        return header.get("payout_batch_id"), header.get("batch_status")
    return body.get("id"), body.get("status")


@dataclass
class PayPalResponse:
    status_code: int
    body: Any
    headers: httpx.Headers
    request_id: str | None
    debug_id: str | None
    ledger_entry: LedgerEntry | None = None


@dataclass
class _Token:
    value: str
    expires_at: float


class PayPalClient:
    """One client per REST app (merchant or operator)."""

    def __init__(
        self,
        credentials: Credentials,
        settings: HttpSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        ledger: RequestLedger | None = None,
    ) -> None:
        self.credentials = credentials
        self.settings = settings
        self.ledger = ledger
        self._sleep = sleep
        self._clock = clock
        self._token: _Token | None = None
        self._http = httpx.Client(
            base_url=settings.api_base,
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PayPalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- OAuth -----------------------------------------------------------

    def _access_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token.expires_at:
            return self._token.value
        resp = self._send_with_retry(
            lambda: self._http.post(
                "/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(self.credentials.client_id, self.credentials.client_secret),
                headers={"Accept": "application/json"},
            )
        )
        if resp.status_code != 200:
            raise PayPalError.from_response(resp)
        body = resp.json()
        ttl = int(body["expires_in"]) - self.settings.token_refresh_margin_seconds
        self._token = _Token(value=body["access_token"], expires_at=now + max(ttl, 0))
        return self._token.value

    # ---- Core request ----------------------------------------------------

    def _send_with_retry(self, send: Callable[[], httpx.Response]) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = send()
            except httpx.TransportError:
                if attempt >= self.settings.max_retries:
                    raise
            else:
                if resp.status_code < 500 or attempt >= self.settings.max_retries:
                    return resp
            delay = min(
                self.settings.backoff_base_seconds * (2**attempt),
                self.settings.backoff_max_seconds,
            )
            self._sleep(delay)
            attempt += 1

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
        operation_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> PayPalResponse:
        method = method.upper()
        headers = {"Accept": "application/json"}
        entry: LedgerEntry | None = None
        if method == "POST":
            if self.ledger is not None:
                if request_id is not None:
                    raise ValueError("with a ledger, request ids come from the ledger")
                entry = self.ledger.begin(
                    operation_key or f"adhoc:{uuid.uuid4()}",
                    self.credentials.label,
                    method,
                    path,
                )
                if entry.state is OpState.SUCCEEDED:
                    raise AlreadyCompleted(entry)
                request_id = entry.request_id
                self.ledger.mark_attempt(entry.operation_key)
            # Same id on every retry, so a retried POST can never double-charge.
            request_id = request_id or str(uuid.uuid4())
            headers["PayPal-Request-Id"] = request_id
            headers["Content-Type"] = "application/json"
            headers["Prefer"] = "return=representation"
        if extra_headers:
            headers.update(extra_headers)

        def send() -> httpx.Response:
            headers["Authorization"] = f"Bearer {self._access_token()}"
            return self._http.request(
                method,
                path,
                json=json if json is not None else ({} if method == "POST" else None),
                params=params,
                headers=headers,
            )

        # A transport error that survives retries propagates and leaves the ledger entry
        # PENDING: the outcome is unknown, and the next attempt reuses the same request id.
        resp = self._send_with_retry(send)
        if resp.status_code == 401 and self._token is not None:
            # Token revoked or expired early: refresh once and retry.
            self._token = None
            resp = self._send_with_retry(send)
        if not 200 <= resp.status_code < 300:
            err = PayPalError.from_response(resp)
            if entry is not None and self.ledger is not None:
                self.ledger.fail(
                    entry.operation_key,
                    http_status=resp.status_code,
                    error=f"{err.name}: {','.join(err.issues) or err.message}",
                    debug_id=err.debug_id,
                )
            raise err
        body: Any = None
        if resp.content:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
        debug_id = resp.headers.get("paypal-debug-id")
        if entry is not None and self.ledger is not None:
            rid, rstatus = resource_ref(body)
            entry = self.ledger.succeed(
                entry.operation_key,
                http_status=resp.status_code,
                resource_id=rid,
                resource_status=rstatus,
                debug_id=debug_id,
            )
        return PayPalResponse(
            status_code=resp.status_code,
            body=body,
            headers=resp.headers,
            request_id=request_id,
            debug_id=debug_id,
            ledger_entry=entry,
        )

    def get(self, path: str, **kw: Any) -> PayPalResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> PayPalResponse:
        return self.request("POST", path, **kw)
