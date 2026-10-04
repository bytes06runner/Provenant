"""S7: negative testing with the PayPal-Mock-Response header on create order, authorize,
capture and refund.

For every (operation, mock code) case in config/spikes.yaml this proves:
  * whether the sandbox honors the mock (the returned issue code equals the mocked code)
  * the request ledger records FAILED with the issue code and no resource id
  * the real resource is unchanged (GET before and after)

Cases are aimed at resources whose real outcome is a *different* error, so a mock that is
ignored can neither be mistaken for a pass nor move money. See config/spikes.yaml.

Run: python spikes/s7_negative_testing.py --authorization-id <voided auth> --capture-id <capture>
"""

from __future__ import annotations

import json
import sys
import uuid
from decimal import Decimal
from typing import Any

from _common import (
    ConfigError,
    PayPalClient,
    PayPalError,
    Spike,
    make_parser,
    merchant_client,
    money,
    order_body,
    session_binding,
    spike_config,
)

from paypal.ledger import OpState


def mock_header(code: str) -> dict[str, str]:
    return {"PayPal-Mock-Response": json.dumps({"mock_application_codes": code})}


def snapshot(client: PayPalClient, path: str) -> dict[str, Any]:
    """State that must not change when a mocked call fails."""
    body = client.get(path).body
    payments = (body.get("purchase_units") or [{}])[0].get("payments", {})
    return {
        "status": body.get("status"),
        "refunds": len(payments.get("refunds", [])),
        "captures": len(payments.get("captures", [])),
    }


def run_case(
    client: PayPalClient,
    spike: Spike,
    run_id: str,
    operation: str,
    code: str,
    path: str,
    body: dict[str, Any],
    state_path: str | None,
) -> dict[str, Any]:
    op_key = f"s7:{run_id}:{operation}:{code}"
    before = snapshot(client, state_path) if state_path else None
    result: dict[str, Any] = {"operation": operation, "mock": code}
    try:
        resp = client.post(path, json=body, operation_key=op_key, extra_headers=mock_header(code))
        spike.step(f"{operation} mock={code}", resp, note="UNEXPECTED SUCCESS")
        result.update(http=resp.status_code, issue=None, outcome="no error")
    except PayPalError as e:
        spike.error(f"{operation} mock={code} (expected error)", e)
        issue = e.issues[0] if e.issues else e.name
        if issue == code:
            outcome = "honored"
        elif e.status_code == 403 and e.body in ("", None):
            outcome = "unsupported"  # 403 with empty body: code not mockable on this endpoint
        else:
            outcome = "ignored"  # PayPal ran the real request and returned its real error
        result.update(http=e.status_code, issue=issue, debug_id=e.debug_id, outcome=outcome)
    print(f"  -> {operation} {code}: {result['outcome']}")

    # Hard requirements: a failed call never looks like success and never leaves a resource id.
    spike.check(
        f"{operation}/{code}: call failed",
        result["outcome"] != "no error",
        f"HTTP {result['http']} issue={result['issue']} outcome={result['outcome']}",
    )
    entry = client.ledger.get(op_key) if client.ledger else None
    expect_in_error = code if result["outcome"] == "honored" else ""
    spike.check(
        f"{operation}/{code}: ledger FAILED, no resource id, error recorded",
        entry is not None
        and entry.state is OpState.FAILED
        and entry.resource_id is None
        and bool(entry.error)
        and expect_in_error in (entry.error or ""),
        f"state={entry.state if entry else None} error={entry.error if entry else None}",
    )

    if state_path:
        after = snapshot(client, state_path)
        spike.check(
            f"{operation}/{code}: resource unchanged", after == before, f"{before} -> {after}"
        )
    return result


def main() -> int:
    parser = make_parser(__doc__ or "")
    parser.add_argument("--authorization-id", help="a VOIDED authorization (capture cases)")
    parser.add_argument("--capture-id", help="a COMPLETED capture (refund cases)")
    args = parser.parse_args()
    cfg = spike_config()
    s7 = cfg["s7"]
    spike = Spike("S7")
    try:
        client = merchant_client(args.merchant or cfg["merchant"])
    except ConfigError as e:
        print(f"Config error: {e}")
        return 2

    run_id = uuid.uuid4().hex[:12]
    results: list[dict[str, Any]] = []
    try:
        with client:
            for code in s7["cases"]["create_order"]:
                custom_id, invoice_id = session_binding()
                results.append(
                    run_case(
                        client,
                        spike,
                        run_id,
                        "create_order",
                        code,
                        "/v2/checkout/orders",
                        order_body(cfg, custom_id, invoice_id),
                        None,
                    )
                )

            # A real, unapproved order: authorizing it for real would fail ORDER_NOT_APPROVED.
            custom_id, invoice_id = session_binding()
            order = client.post(
                "/v2/checkout/orders",
                json=order_body(cfg, custom_id, invoice_id),
                operation_key=f"s7:{run_id}:setup-order",
            )
            spike.step("setup: unapproved order", order)
            oid = order.body["id"]
            for code in s7["cases"]["authorize"]:
                results.append(
                    run_case(
                        client,
                        spike,
                        run_id,
                        "authorize",
                        code,
                        f"/v2/checkout/orders/{oid}/authorize",
                        {},
                        f"/v2/checkout/orders/{oid}",
                    )
                )

            if args.authorization_id:
                auth_path = f"/v2/payments/authorizations/{args.authorization_id}"
                for code in s7["cases"]["capture"]:
                    results.append(
                        run_case(
                            client,
                            spike,
                            run_id,
                            "capture",
                            code,
                            f"{auth_path}/capture",
                            {"final_capture": True},
                            auth_path,
                        )
                    )
            else:
                print("\nSkipping capture cases: no --authorization-id")

            if args.capture_id:
                cap_path = f"/v2/payments/captures/{args.capture_id}"
                cap = client.get(cap_path).body
                too_much = Decimal(cap["amount"]["value"]) * int(s7["refund_amount_multiplier"])
                body = {"amount": money(too_much, cap["amount"]["currency_code"])}
                for code in s7["cases"]["refund"]:
                    results.append(
                        run_case(
                            client,
                            spike,
                            run_id,
                            "refund",
                            code,
                            f"{cap_path}/refund",
                            body,
                            cap_path,
                        )
                    )
            else:
                print("\nSkipping refund cases: no --capture-id")
    except PayPalError as e:
        spike.error("setup call", e)
        spike.check("no PayPal error in setup", False, str(e))
    except Exception as e:
        spike.crashed(e)

    # The mechanism must work on every operation we exercised, with at least one code each.
    for op in dict.fromkeys(r["operation"] for r in results):
        honored = [r["mock"] for r in results if r["operation"] == op and r["outcome"] == "honored"]
        spike.check(f"{op}: mock header works for at least one code", bool(honored), str(honored))

    spike.log.append({"step": "summary", "results": results})
    print("\nSummary (operation, mock, HTTP, returned issue, outcome):")
    for r in results:
        print(f"  {r['operation']:<13} {r['mock']:<34} {r['http']}  {r['issue']}  {r['outcome']}")
    return spike.finish()


if __name__ == "__main__":
    sys.exit(main())
