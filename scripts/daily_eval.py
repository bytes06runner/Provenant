"""Daily queue for attack success and utility (config/eval/security.yaml, docs/eval-plan.md).

  python scripts/daily_eval.py                 # today's share, then exit
  python scripts/daily_eval.py --max 1         # one task (both agents), for a check
  python scripts/daily_eval.py --summary       # rebuild eval/results/security/summary.json

Runs pending tasks in order (attacks first, then benign) while Flash-Lite has room: at most
`flash_lite_allowance` requests for this queue per Gemini day, never below `flash_lite_reserve`,
and no new task after `stop_starting_after_utc`. Each task runs the baseline and Provenant on the
same planted page. Started daily by launchd (scripts/launchd/install.sh).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from baseline.agent import BaselineDeps, run_baseline, toolkit_create_order  # noqa: E402
from eval import security as sec  # noqa: E402
from lab import placement  # noqa: E402
from lab.generate import latest_dataset  # noqa: E402
from lineage.runtime import Runtime  # noqa: E402
from llm import roles  # noqa: E402
from paypal.config import load_yaml  # noqa: E402

LOG = REPO_ROOT / "var" / "daily-eval.log"


def log(msg: str) -> None:
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def build_deps(rt: Runtime, cfg: dict[str, Any]) -> tuple[sec.SecurityDeps, list[sec.Task]]:
    user = cfg["user"]
    toolbox = rt.toolbox(lambda _t, _p: None)
    catalogs = {m: toolbox.fetch_manifest(m) for m in rt.records}
    attacker_payee = rt.records["attacker"].paypal_merchant_id
    fill = placement.fill_values(attacker_payee, load_yaml("attacks/taxonomy.yaml"))
    dataset = latest_dataset()
    if dataset is None:
        raise SystemExit("no attack dataset: run python -m lab.generate")
    bcfg = load_yaml("baseline.yaml")
    address = dict(next(iter(rt.users[user]["addresses"].values())))
    addresses = sorted(rt.users[user]["addresses"])
    model = cfg["daily"]["flash_lite_model"]

    def run_baseline_task(task: sec.Task) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        session = f"b-{uuid.uuid4().hex[:12]}"

        def record(event_type: str, payload: dict[str, Any]) -> None:
            rt.recorder.append(session, event_type, payload)

        rt.record_into(record)
        record("eval.task", {"task": task.id, "kind": task.kind})
        deps = BaselineDeps(
            router=rt.router,
            merchants={k: r.base_url for k, r in rt.records.items()},
            http=rt.http,
            create_order=toolkit_create_order(rt.credentials, str(bcfg["toolkit_source"])),
            record=record,
            settings=bcfg,
            read_order=lambda m, oid: rt.paypal(m).get(f"/v2/checkout/orders/{oid}").body,
        )
        run = run_baseline(deps, task.request, address)
        order_id = (run.order or {}).get("order", {}).get("id")
        pages = [str(st["args"].get("url", "")) for st in run.steps if st["tool"] == "open_page"]
        return run.order, {"session": session, "order_id": order_id, "pages": pages}

    def run_provenant_task(task: sec.Task, url: str | None) -> tuple[Any, dict[str, Any]]:
        s = rt.new_session(user)
        s.record("eval.task", {"task": task.id, "kind": task.kind})
        proposal = s.propose(task.request, addresses)
        fields = dict(proposal.fields)
        for k, v in task.answers.items():
            if fields.get(k) is None:
                fields[k] = v
        problems = roles._check_proposal(  # noqa: SLF001
            fields, rt.spec["attributes"], addresses, sorted(rt.records)
        )
        if problems:
            raise RuntimeError(f"mandate problems: {problems}")
        vm = s.confirm_and_sign(fields)
        p = s.run(vm, s.plan(vm, url))
        return p, {"session": s.session_id}

    def plant(task: sec.Task) -> str:
        assert task.attack is not None and task.host and task.surface
        host = rt.records[task.host]
        return placement.plant(
            rt.http,
            host.base_url,
            host=task.host,
            surface=task.surface,
            attack=task.attack,
            fill=fill,
            first_sku=catalogs[task.host].manifest.catalog[0].sku,
        )

    deps = sec.SecurityDeps(
        run_baseline=run_baseline_task,
        run_provenant=run_provenant_task,
        plant=plant,
        clear=lambda: placement.clear(rt.http, next(iter(rt.records.values())).base_url),
        catalogs=catalogs,
        attacker_payee=attacker_payee,
        flash_lite_used=lambda: rt.router.budget.spend(model).requests_today,
    )
    return deps, sec.build_tasks(cfg, dataset["attacks"], catalogs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--max", type=int)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if args.summary:
        print(json.dumps(sec.write_summary(), indent=2))
        return 0
    cfg = load_yaml("eval/security.yaml")
    d = cfg["daily"]
    rt = Runtime.load()
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from nightly_eval import ensure_simulator  # same check: start the simulator if it is down

    ensure_simulator(os.environ["MERCHANTS_BASE_URL"].rstrip("/"))
    deps, tasks = build_deps(rt, cfg)
    done = sec.done_tasks(sec.load_rows())
    pending = [t for t in tasks if t.id not in done]
    lim = rt.router.budget.limits_for(d["flash_lite_model"])
    start_used, ran = deps.flash_lite_used(), 0
    stop_h, stop_m = _hhmm(d["stop_starting_after_utc"])
    log(
        f"start: {len(pending)} of {len(tasks)} tasks pending; "
        f"Flash-Lite at {start_used} of {lim.requests_per_day}"
    )
    for task in pending:
        if args.max is not None and ran >= args.max:
            break
        now = datetime.now(UTC)
        if args.max is None and (now.hour, now.minute) >= (stop_h, stop_m):
            log("stop: past the start window")
            break
        used = deps.flash_lite_used()
        need = int(d["estimate_flash_lite_per_task"])
        room = min(
            int(d["flash_lite_allowance"]) - (used - start_used),
            lim.requests_per_day - int(d["flash_lite_reserve"]) - used,
        )
        if need > room:
            log(f"stop: {task.id} needs about {need} Flash-Lite requests, {room} left today")
            break
        log(f"run {task.id}")
        try:
            rows = sec.run_task(task, deps)
        except Exception:  # noqa: BLE001  (placement failed; skip and keep going)
            log(traceback.format_exc(limit=3))
            continue
        sec.append(rows)
        ran += 1
        log(
            " ".join(
                f"{r['agent']}: attack={r['attack_success']} task={r['task_success']} "
                f"fl={r['flash_lite_calls']}"
                for r in rows
            )
        )
    s = sec.write_summary()
    log(
        f"summary: baseline ASR {s['baseline']['asr']}, "
        f"Provenant ASR {s['provenant']['asr']}; ran {ran}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
