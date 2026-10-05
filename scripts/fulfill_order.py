"""Merchant ships an authorized order; the payment is captured and tracking added.

Run: python scripts/fulfill_order.py --session s-xxxxxxxxxxxx [--planted-wrong-variant]
(--planted-wrong-variant needs the simulator started with SIMULATOR_ALLOW_PLANTED=1)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lineage.fulfillment import fulfill_and_capture  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True)
    ap.add_argument("--planted-wrong-variant", action="store_true")
    args = ap.parse_args()
    rt = Runtime.load()
    created = next(
        e for e in rt.recorder.events(args.session) if e.event_type == "paypal.order.created"
    )
    merchant = created.payload["merchant_id"]
    out = fulfill_and_capture(
        recorder=rt.recorder,
        session_id=args.session,
        storefront_url=rt.records[merchant].base_url,
        http=rt.http,
        paypal=rt.paypal,
        force_wrong_variant=args.planted_wrong_variant,
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
