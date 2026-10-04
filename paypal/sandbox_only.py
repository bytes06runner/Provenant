"""Sandbox-only PayPal endpoints, isolated here (CLAUDE.md section 5.4).

These exist only in the PayPal sandbox to drive a dispute through states a real buyer and
PayPal agent would otherwise drive. They are used by the demo and test harness, never by the
application. Every call refuses to run against any host other than the sandbox API.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

from paypal.client import PayPalClient, PayPalResponse

SANDBOX_HOSTS = {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}


class NotSandboxError(RuntimeError):
    pass


class AdjudicationOutcome(StrEnum):
    BUYER_FAVOR = "BUYER_FAVOR"
    SELLER_FAVOR = "SELLER_FAVOR"


def _require_sandbox(client: PayPalClient) -> None:
    host = urlparse(client.settings.api_base).hostname
    if host not in SANDBOX_HOSTS:
        raise NotSandboxError(f"sandbox-only endpoint refused for host {host!r}")


def adjudicate(
    client: PayPalClient, dispute_id: str, outcome: AdjudicationOutcome, *, operation_key: str
) -> PayPalResponse:
    """Settle a dispute that is UNDER_REVIEW, as a PayPal agent would."""
    _require_sandbox(client)
    return client.post(
        f"/v1/customer/disputes/{dispute_id}/adjudicate",
        json={"adjudication_outcome": outcome.value},
        operation_key=operation_key,
    )


def require_evidence(
    client: PayPalClient, dispute_id: str, action: str, *, operation_key: str
) -> PayPalResponse:
    """Move a dispute to waiting-for-evidence. action: BUYER_EVIDENCE or SELLER_EVIDENCE."""
    _require_sandbox(client)
    return client.post(
        f"/v1/customer/disputes/{dispute_id}/require-evidence",
        json={"action": action},
        operation_key=operation_key,
    )
