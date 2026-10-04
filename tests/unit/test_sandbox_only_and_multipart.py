"""Sandbox-only guard and multipart uploads."""

from __future__ import annotations

import httpx
import pytest

from paypal.client import PayPalClient
from paypal.config import Credentials, HttpSettings
from paypal.sandbox_only import AdjudicationOutcome, NotSandboxError, adjudicate, require_evidence

CREDS = Credentials(label="merchant:test", client_id="cid-123456", client_secret="shh")


def client(base: str, seen: list[httpx.Request]) -> PayPalClient:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append(request)
        return httpx.Response(200, json={"links": []})

    return PayPalClient(
        CREDS, HttpSettings(base, 5, 0, 0, 0, 60), transport=httpx.MockTransport(transport)
    )


@pytest.mark.parametrize(
    "base", ["https://api-m.paypal.com", "https://api.paypal.com", "https://evil.test"]
)
def test_sandbox_only_calls_refuse_non_sandbox_hosts(base):
    seen: list[httpx.Request] = []
    c = client(base, seen)
    with pytest.raises(NotSandboxError):
        adjudicate(c, "PP-D-1", AdjudicationOutcome.BUYER_FAVOR, operation_key="k1")
    with pytest.raises(NotSandboxError):
        require_evidence(c, "PP-D-1", "SELLER_EVIDENCE", operation_key="k2")
    assert seen == []


def test_sandbox_only_calls_run_against_sandbox():
    seen: list[httpx.Request] = []
    c = client("https://api-m.sandbox.paypal.com", seen)
    adjudicate(c, "PP-D-1", AdjudicationOutcome.SELLER_FAVOR, operation_key="k1")
    require_evidence(c, "PP-D-1", "SELLER_EVIDENCE", operation_key="k2")
    assert [r.url.path for r in seen] == [
        "/v1/customer/disputes/PP-D-1/adjudicate",
        "/v1/customer/disputes/PP-D-1/require-evidence",
    ]


def test_multipart_upload_keeps_idempotency_and_sets_boundary():
    seen: list[httpx.Request] = []
    c = client("https://api-m.sandbox.paypal.com", seen)
    c.post(
        "/v1/customer/disputes/PP-D-1/provide-evidence",
        files={
            "input": (None, '{"evidences": []}', "application/json"),
            "file1": ("pack.pdf", b"%PDF-1.4", "application/pdf"),
        },
    )
    (req,) = seen
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert req.headers["PayPal-Request-Id"] and req.headers["Prefer"] == "return=representation"
    body = req.read()
    assert b'name="input"' in body and b'filename="pack.pdf"' in body and b"%PDF-1.4" in body


def test_json_and_files_together_rejected():
    c = client("https://api-m.sandbox.paypal.com", [])
    with pytest.raises(ValueError):
        c.post("/x", json={}, files={"f": ("a", b"b", "text/plain")})
