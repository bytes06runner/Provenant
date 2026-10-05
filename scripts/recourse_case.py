"""Run a recourse case on a recorded purchase: facts, replay, attribution, remedy, ruling.

  python scripts/recourse_case.py --session s-... --text "..." \
      [--clarify required.waterproof=yes] [--clarify preference=lowest_total] \
      [--report waterproof=no] [--photo-color black] [--k 4 | --demo] [--approve]

--photo-color renders a synthetic photo of the item in that color (there is no camera here).
Without --approve nothing moves on PayPal: the remedy is only proposed (AUTO_REMEDY=false).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from blackbox.case import run_case  # noqa: E402
from blackbox.intake import Complaint  # noqa: E402
from blackbox.replay import ReplaySettings  # noqa: E402
from lab.synthetic import COLORS, sneaker_photo  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402


def parse_clarify(items: list[str], base: dict[str, Any]) -> dict[str, Any]:
    edits: dict[str, Any] = {}
    for item in items:
        key, _, value = item.partition("=")
        if key.startswith("required."):
            req = dict(edits.get("required_attributes", base["required_attributes"]))
            req[key.split(".", 1)[1]] = value
            edits["required_attributes"] = req
        elif key.startswith("forbidden."):
            forb = dict(edits.get("forbidden_attributes", base["forbidden_attributes"]))
            forb[key.split(".", 1)[1]] = value.split(",")
            edits["forbidden_attributes"] = forb
        else:
            edits[key] = json.loads(value) if value[:1] in '[{"0123456789' else value
    return edits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--session", required=True)
    ap.add_argument("--user", default="buyer_a")
    ap.add_argument("--text", required=True)
    ap.add_argument("--clarify", action="append", default=[])
    ap.add_argument("--report", action="append", default=[])
    ap.add_argument("--photo-color", choices=sorted(COLORS))
    ap.add_argument("--k", type=int)
    ap.add_argument("--demo", action="store_true", help="use the demo k (8)")
    ap.add_argument("--approve", action="store_true", help="operator approves the remedy")
    args = ap.parse_args()

    rt = Runtime.load()
    signed = next(e for e in rt.recorder.events(args.session) if e.event_type == "mandate.signed")
    base = signed.payload["envelope"]["payload"]
    deps = rt.case_deps(args.user)
    k = ReplaySettings.for_demo().k if args.demo else ReplaySettings.for_run(args.k).k
    complaint = Complaint(
        text=args.text,
        clarified=parse_clarify(args.clarify, base),
        reported_attributes=dict(x.split("=", 1) for x in args.report),
        photo_png=sneaker_photo(args.photo_color) if args.photo_color else None,
    )
    started = time.monotonic()
    r = run_case(deps, args.session, complaint, k=k, approve=args.approve)
    print(
        json.dumps(
            {
                "case_id": r.case_id,
                "k": k,
                "seconds": round(time.monotonic() - started),
                "facts": r.facts.to_dict(),
                "attribution": r.attribution,
                "remedy": r.plan.to_dict(),
                "ruling": r.ruling,
                "executed": r.executed,
                "settled": r.settled,
                "discrepancies": r.discrepancies,
                "evidence_pack": str(r.evidence_pdf),
                "case_chain_head": rt.recorder.verify(r.case_id),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
