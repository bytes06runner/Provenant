"""Nightly attribution-eval queue (config/eval/attribution.yaml, section `nightly`).

  python scripts/nightly_eval.py                 # run tonight's share of the queue, then exit
  python scripts/nightly_eval.py --max 1         # a single instance (development)
  python scripts/nightly_eval.py --forecast 2026-11-05

Runs instances in round robin while the budget model has room: at most `allowance_tokens`
for the queue per UTC day, never below `reserve_tokens`, and no new instance after
`stop_starting_after_utc`. Waits for other heavy jobs (recourse cases) to finish first. Each
finished instance appends one line to eval/results/attribution/results.jsonl and refreshes
summary.json. Started daily by launchd (scripts/launchd/com.provenant.nightly-eval.plist).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.attribution import (  # noqa: E402
    RESULTS,
    EvalDeps,
    Instance,
    append,
    load_rows,
    next_instance,
    run_instance,
    write_summary,
)
from lab.synthetic import sneaker_photo  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402
from llm.types import AllTargetsExhausted  # noqa: E402
from paypal.config import load_yaml  # noqa: E402

LOG = REPO_ROOT / "var" / "nightly-eval.log"
HEAVY = ("recourse_case.py", "planted_scenarios.py", "after_reset.sh")


def log(msg: str) -> None:
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def other_heavy_jobs() -> list[str]:
    out = subprocess.run(["/bin/ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
    me = str(os.getpid())
    return [
        line.strip()
        for line in out.splitlines()
        if any(h in line for h in HEAVY) and not line.strip().startswith(me)
    ]


def ensure_simulator(base_url: str) -> None:
    import httpx

    try:
        if httpx.get(f"{base_url}/healthz", timeout=5).status_code == 200:
            return
    except httpx.HTTPError:
        pass
    port = base_url.rsplit(":", 1)[-1].split("/")[0]
    log(f"merchant simulator down, starting it on port {port}")
    env = {**os.environ, "SIMULATOR_ALLOW_PLANTED": "1"}
    subprocess.Popen(  # noqa: S603
        [
            str(REPO_ROOT / ".venv/bin/uvicorn"),
            "merchants.app:app",
            "--port",
            port,
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(30):
        time.sleep(1)
        try:
            if httpx.get(f"{base_url}/healthz", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            continue
    raise SystemExit("merchant simulator did not start")


def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def forecast(
    cfg: dict[str, Any], until: date, start: date, done: int, first_night_room: int | None
) -> dict[str, Any]:
    """Instances the queue can finish by `until`, from the planning estimates."""
    n = cfg["nightly"]
    allowance = int(n["allowance_tokens"])
    est = n["estimate_tokens"]
    counts: dict[str, int] = {}
    nights = (until - start).days + 1
    for night in range(nights):
        room = allowance if night or first_night_room is None else min(allowance, first_night_room)
        while True:
            inst = next_instance(cfg, done)
            need = int(est[inst.kind])
            if need > room:
                break
            room -= need
            counts[inst.kind] = counts.get(inst.kind, 0) + 1
            done += 1
    return {"nights": nights, "instances": sum(counts.values()), "by_kind": counts}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--max", type=int, help="stop after this many instances")
    ap.add_argument("--forecast", type=date.fromisoformat, help="estimate instances by this date")
    ap.add_argument("--first-night-room", type=int, help="forecast: tokens free on the first night")
    ap.add_argument("--kind", help="smoke test: run this scenario type (variant 0) once")
    ap.add_argument("--results", type=Path, default=RESULTS, help="results directory")
    args = ap.parse_args()
    cfg = load_yaml("eval/attribution.yaml")
    n = cfg["nightly"]
    if args.forecast:
        now = datetime.now(UTC)
        first = now.date() + timedelta(
            days=1 if (now.hour, now.minute) >= _hhmm(n["start_utc"]) else 0
        )
        out = forecast(cfg, args.forecast, first, len(load_rows()), args.first_night_room)
        print(json.dumps(out, indent=2))
        return 0

    if args.kind:
        args.max = 1
    while not args.kind and (jobs := other_heavy_jobs()):
        log(f"waiting for {len(jobs)} running job(s): {jobs[0][:80]}")
        time.sleep(60)
    rt = Runtime.load()
    ensure_simulator(os.environ["MERCHANTS_BASE_URL"].rstrip("/"))
    model = n["model"]
    lim = rt.router.budget.limits_for(model)

    def used() -> int:
        return rt.router.budget.spend(model).tokens_today

    deps = EvalDeps(
        new_session=rt.new_session,
        case_deps=rt.case_deps(cfg["user"]),
        records=rt.records,
        http=rt.http,
        addresses=list(rt.users[cfg["user"]]["addresses"]),
        vocabulary=rt.spec["attributes"],
        photo=sneaker_photo,
        tokens_used=used,
    )
    stop_h, stop_m = _hhmm(n["stop_starting_after_utc"])
    spent, ran, errors = 0, 0, 0
    log(f"start: {model} at {used()} of {lim.tokens_per_day} tokens today")
    while args.max is None or ran < args.max:
        now = datetime.now(UTC)
        if not args.kind and (now.hour, now.minute) >= (stop_h, stop_m):
            log("stop: past the start window")
            break
        done = len(load_rows(args.results))
        inst = Instance(args.kind, 0, done) if args.kind else next_instance(cfg, done)
        need = int(n["estimate_tokens"][inst.kind])
        room = min(
            int(n["allowance_tokens"]) - spent,
            lim.tokens_per_day - int(n["reserve_tokens"]) - used(),
        )
        if need > room and not args.kind:
            log(f"stop: {inst.id} needs about {need} tokens, {room} left for the queue tonight")
            break
        log(f"run {inst.id}")
        try:
            row = run_instance(cfg, inst, deps)
            errors = 0
        except AllTargetsExhausted as e:
            log(f"stop: budget exhausted during {inst.id}: {e}")
            break
        except Exception as e:  # noqa: BLE001  (record and keep the queue moving)
            errors += 1
            row = {
                "instance": inst.id,
                "kind": inst.kind,
                "variant": inst.variant,
                "valid": False,
                "invalid": f"error: {type(e).__name__}: {str(e)[:300]}",
                "at": now.isoformat(timespec="seconds"),
                "budget_tokens": 0,
            }
            log(traceback.format_exc(limit=3))
        append(row, args.results)
        spent += int(row.get("budget_tokens", 0))
        ran += 1
        log(
            f"done {inst.id}: valid={row.get('valid')} correct={row.get('correct')} "
            f"tokens={row.get('budget_tokens')} {row.get('invalid', '')}"
        )
        if errors >= 2:
            log("stop: two errors in a row")
            break
    summary = write_summary(args.results)
    log(f"summary: {json.dumps(summary.get('overall'))}; queue spent {spent} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
