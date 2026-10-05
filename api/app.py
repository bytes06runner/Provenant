"""Provenant API for the buyer app (Phase 3, Lineage screens first).

    uvicorn api.app:app --port 8700

Flow: POST /api/sessions (draft mandate from the request) -> POST /confirm (sign) -> POST /run
(plan and interpreter in the background; the run view polls /events) -> GET /provenance ->
POST /order (PayPal order) -> PayPal button (JS SDK v6, client token from /api/paypal/client-token)
-> POST /authorize. Every step lands in the Flight Recorder; this module only orchestrates and
reads it. Steps that call models run one at a time: the router records into one active session.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.views import provenance_graph, short, step
from lineage.fulfillment import FulfillmentError, authorize_recorded
from lineage.interpreter import Proposal
from lineage.mandate import VerifiedMandate
from lineage.probes import payee_swap
from lineage.purchase import OrderResult, PurchaseSession
from lineage.runtime import Runtime
from llm import roles
from paypal.client import PayPalError
from paypal.config import load_yaml


@dataclass
class Entry:
    session: PurchaseSession
    user: str
    fields: dict[str, Any] = field(default_factory=dict)
    vm: VerifiedMandate | None = None
    proposal: Proposal | None = None
    order: OrderResult | None = None
    state: str = (
        "drafted"  # drafted | signed | running | proposed | blocked | ordered | authorized | failed
    )
    error: str | None = None


class SessionRequest(BaseModel):
    user: str
    request: str


class ConfirmRequest(BaseModel):
    fields: dict[str, Any]


class RunRequest(BaseModel):
    review_sku: str | None = None  # merchant/sku whose reviews the planner may read
    probe_attacks: bool = True


def create_app(rt: Runtime | None = None) -> FastAPI:
    rt = rt or Runtime.load()
    web = load_yaml("app.yaml")["web"]
    app = FastAPI(title="Provenant API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(web["cors_origins"]),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    sessions: dict[str, Entry] = {}
    model_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=8)

    def entry(sid: str) -> Entry:
        e = sessions.get(sid)
        if e is None:
            raise HTTPException(404, f"no live session {sid}")
        return e

    def chain_head(sid: str) -> str | None:
        h = rt.recorder.head(sid)
        return h.event_hash if h else None

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "users": [
                {"id": uid, "name": u["display_name"], "addresses": sorted(u["addresses"])}
                for uid, u in rt.users.items()
            ],
            "merchants": sorted(rt.records),
            "vocabulary": rt.spec["attributes"],
            "category": rt.spec["category"],
            "sdk_script_url": web["sdk_script_url"],
            "presentation_mode": web["presentation_mode"],
        }

    # ---- 1. mandate ----------------------------------------------------------------------

    @app.post("/api/sessions")
    def create_session(body: SessionRequest) -> dict[str, Any]:
        if body.user not in rt.users:
            raise HTTPException(404, f"unknown user {body.user}")
        if not body.request.strip():
            raise HTTPException(422, "the request is empty")
        with model_lock:
            s = rt.new_session(body.user)
            proposal = s.propose(body.request, sorted(rt.users[body.user]["addresses"]))
        e = Entry(s, body.user, dict(proposal.fields))
        sessions[s.session_id] = e
        return {
            "id": s.session_id,
            "fields": proposal.fields,
            "questions": proposal.questions,
            "problems": proposal.problems,
            "ready": proposal.ready,
        }

    @app.post("/api/sessions/{sid}/confirm")
    def confirm(sid: str, body: ConfirmRequest) -> dict[str, Any]:
        e = entry(sid)
        if e.state != "drafted":
            raise HTTPException(409, f"session is {e.state}")
        problems = roles._check_proposal(  # noqa: SLF001
            body.fields,
            rt.spec["attributes"],
            sorted(rt.users[e.user]["addresses"]),
            sorted(rt.records),
        )
        if problems:
            raise HTTPException(422, {"problems": problems})
        try:
            e.vm = e.session.confirm_and_sign(body.fields)
        except ValueError as err:
            raise HTTPException(422, {"problems": [str(err)]}) from err
        e.fields, e.state = dict(body.fields), "signed"
        env = e.vm.envelope
        return {
            "mandate": e.vm.mandate.model_dump(mode="json"),
            "hash": env.payload_hash,
            "key_id": env.key_id,
            "seal": short(env.payload_hash, 12),
        }

    # ---- 2. run ------------------------------------------------------------------------

    @app.post("/api/sessions/{sid}/run", status_code=202)
    def run(sid: str, body: RunRequest) -> dict[str, str]:
        e = entry(sid)
        if e.state != "signed" or e.vm is None:
            raise HTTPException(409, f"session is {e.state}")
        e.state = "running"
        review_url = None
        if body.review_sku:
            merchant, _, sku = body.review_sku.partition("/")
            if merchant not in rt.records or not sku:
                raise HTTPException(422, "review_sku must be <merchant>/<sku>")
            review_url = f"{rt.records[merchant].base_url}/reviews/{sku}"

        def work() -> None:
            assert e.vm is not None
            try:
                with model_lock:
                    rt.record_into(e.session.record)
                    plan = e.session.plan(e.vm, review_url)
                    p = e.session.run(e.vm, plan)
                    e.proposal = p
                    if p.result.allowed and body.probe_attacks and "attacker" in rt.records:
                        payee_swap(
                            e.session,
                            e.session.toolbox,
                            e.vm,
                            p,
                            attacker_id="attacker",
                            attacker_base_url=rt.records["attacker"].base_url,
                        )
                e.state = "proposed" if p.result.allowed else "blocked"
            except Exception as err:  # noqa: BLE001  (shown to the user, full trace in the log)
                e.state, e.error = "failed", f"{type(err).__name__}: {err}"
                traceback.print_exc()

        pool.submit(work)
        return {"state": e.state}

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str) -> dict[str, Any]:
        e = sessions.get(sid)
        out: dict[str, Any] = {"id": sid, "chain_head": chain_head(sid)}
        if e is None:
            out["state"] = "archived" if out["chain_head"] else "unknown"
            return out
        out.update({"state": e.state, "error": e.error, "user": e.user, "fields": e.fields})
        if e.vm is not None:
            out["mandate_seal"] = short(e.vm.envelope.payload_hash, 12)
        if e.proposal is not None:
            out["chosen"] = e.proposal.candidate.summary()
            out["decision_label"] = e.proposal.decision_label.name
            out["manifest_seal"] = short(e.proposal.candidate.manifest.hash, 12)
        if e.order is not None:
            out["order"] = {
                "id": e.order.order_id,
                "merchant_id": e.order.merchant_id,
                "status": e.order.status,
                "custom_id": e.order.custom_id,
                "approval_url": e.order.approval_url,
            }
        return out

    @app.get("/api/sessions/{sid}/events")
    def events(sid: str, after: int = -1) -> dict[str, Any]:
        evs = [x for x in rt.recorder.events(sid) if x.seq > after]
        e = sessions.get(sid)
        return {
            "state": e.state if e else "archived",
            "error": e.error if e else None,
            "steps": [step(x.event_type, x.seq, x.recorded_at.isoformat(), x.payload) for x in evs],
        }

    @app.get("/api/sessions/{sid}/provenance")
    def provenance(sid: str) -> dict[str, Any]:
        evs = rt.recorder.events(sid)
        if not evs:
            raise HTTPException(404, f"no recorded session {sid}")
        graph = provenance_graph([(x.event_type, x.payload) for x in evs])
        signed = next((x for x in evs if x.event_type == "mandate.signed"), None)
        graph["mandate_seal"] = (
            short(signed.payload["envelope"]["payload_hash"], 12) if signed else None
        )
        graph["chain_head"] = short(evs[-1].event_hash, 12)
        return graph

    # ---- 3. PayPal ---------------------------------------------------------------------

    @app.post("/api/sessions/{sid}/order")
    def order(sid: str) -> dict[str, Any]:
        e = entry(sid)
        if e.state != "proposed" or e.proposal is None:
            raise HTTPException(409, f"session is {e.state}")
        try:
            e.order = e.session.create_order(e.proposal)
        except PayPalError as err:
            raise HTTPException(502, err.summary()) from err
        e.state = "ordered"
        return {
            "order_id": e.order.order_id,
            "merchant_id": e.order.merchant_id,
            "approval_url": e.order.approval_url,
            "custom_id": e.order.custom_id,
        }

    @app.get("/api/paypal/client-token")
    def client_token(merchant: str) -> dict[str, str]:
        if merchant not in rt.records:
            raise HTTPException(404, f"unknown merchant {merchant}")
        try:
            with rt.paypal(merchant) as c:
                token = c.browser_client_token(list(web["client_token_domains"]))
        except PayPalError as err:
            raise HTTPException(502, err.summary()) from err
        return {"clientToken": token}

    @app.post("/api/sessions/{sid}/authorize")
    def authorize(sid: str) -> dict[str, Any]:
        e = entry(sid)
        if e.order is None:
            raise HTTPException(409, "no PayPal order yet")
        try:
            result = authorize_recorded(
                recorder=rt.recorder,
                session_id=sid,
                paypal=rt.paypal,
                poll_seconds=float(web["approval_poll_seconds"]),
                timeout_seconds=float(web["approval_timeout_seconds"]),
                sleep=time.sleep,
                clock=time.monotonic,
            )
        except (FulfillmentError, PayPalError) as err:
            raise HTTPException(502, str(err)) from err
        e.state = "authorized"
        return result

    # ---- 4. orders ---------------------------------------------------------------------

    @app.get("/api/orders")
    def orders(limit: int = 20) -> dict[str, Any]:
        created = rt.recorder.latest("paypal.order.created", limit=limit)

        def row(ev: Any) -> dict[str, Any]:
            mid, oid = ev.payload["merchant_id"], ev.payload["order"]["id"]
            r: dict[str, Any] = {
                "session": ev.session_id,
                "merchant_id": mid,
                "order_id": oid,
                "created_at": ev.recorded_at.isoformat(),
                "custom_id": ev.payload["order"]["purchase_units"][0].get("custom_id"),
            }
            try:
                with rt.paypal(mid) as c:
                    o = c.get(f"/v2/checkout/orders/{oid}").body
            except PayPalError as err:
                r["status"], r["error"] = "UNKNOWN", err.summary()
                return r
            pu = (o.get("purchase_units") or [{}])[0]
            r["status"] = o.get("status")
            r["amount"] = (pu.get("amount") or {}).get("value")
            r["currency"] = (pu.get("amount") or {}).get("currency_code")
            r["items"] = [i.get("name") for i in pu.get("items", [])]
            pay = pu.get("payments") or {}
            r["payments"] = {
                k: [{"id": x["id"], "status": x.get("status")} for x in v] for k, v in pay.items()
            }
            return r

        return {"orders": list(pool.map(row, created))}

    return app


app = create_app() if os.environ.get("PROVENANT_API_NO_AUTOLOAD") != "1" else FastAPI()
