"""Re-render a recorded recourse case with the current interval method (no new replays).

  python scripts/rerender_case.py --session s-9315c6f5ddf0 [--user buyer_a]

Appends `attribution.revised`, a revised remedy proposal, ruling and evidence pack to the case
chain, and writes var/cases/<case>-<method>.pdf. Nothing moves on PayPal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from blackbox.case import rerender_case  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True)
    ap.add_argument("--user", default="buyer_a")
    args = ap.parse_args()
    rt = Runtime.load()
    r = rerender_case(rt.case_deps(args.user), args.session)
    out = {
        "case": r.case_id,
        "attribution": r.attribution,
        "remedy": r.plan.to_dict(),
        "ruling": r.ruling,
        "evidence_pack": str(r.evidence_pdf),
        "case_chain_head": rt.recorder.verify(r.case_id),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
