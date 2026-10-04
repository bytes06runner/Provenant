"""Thin PayPal REST client.

Responsibilities, and nothing more:
  * OAuth client-credentials token per REST app, cached until shortly before expiry
  * PayPal-Request-Id idempotency key on every POST (caller may pin one to retry safely)
  * Retries with exponential backoff on 5xx and transport errors only
  * Structured errors carrying PayPal's name / issue codes and debug_id
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from paypal.config import Credentials, HttpSettings


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


@dataclass
class PayPalResponse:
    status_code: int
    body: Any
    headers: httpx.Headers
    request_id: str | None
    debug_id: str | None


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
    ) -> None:
        self.credentials = credentials
        self.settings = settings
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
        extra_headers: dict[str, str] | None = None,
    ) -> PayPalResponse:
        method = method.upper()
        headers = {"Accept": "application/json"}
        if method == "POST":
            # Same id on every retry, so a retried POST can never double-charge.
            request_id = request_id or str(uuid.uuid4())
            headers["PayPal-Request-Id"] = request_id
            headers["Content-Type"] = "application/json"
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

        resp = self._send_with_retry(send)
        if resp.status_code == 401 and self._token is not None:
            # Token revoked or expired early: refresh once and retry.
            self._token = None
            resp = self._send_with_retry(send)
        if resp.status_code >= 400:
            raise PayPalError.from_response(resp)
        body: Any = None
        if resp.content:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
        return PayPalResponse(
            status_code=resp.status_code,
            body=body,
            headers=resp.headers,
            request_id=request_id,
            debug_id=resp.headers.get("paypal-debug-id"),
        )

    def get(self, path: str, **kw: Any) -> PayPalResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> PayPalResponse:
        return self.request("POST", path, **kw)
