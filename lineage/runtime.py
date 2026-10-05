"""Wires Provenant's pieces from config and .env: storage, LLM router, merchant registry,
toolbox, vault, keys and PayPal clients. Used by the demo scripts, the sandbox integration test
and Blackbox, so every entry point builds a purchase session the same way.

State lives under `var/` (SQLite until the Postgres stores land just before deploy).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from blackbox.recorder import CustomIdFormat, FlightRecorder
from lineage.manifest import MerchantKeyRegistry
from lineage.nonces import SqlNonceRegistry
from lineage.purchase import PurchaseSession
from lineage.signing import key_id
from lineage.toolbox import HttpToolbox
from lineage.vault import Address, AddressVault
from llm import build_router
from llm.router import LLMRouter
from merchants.keystore import load_or_create
from merchants.registry import MerchantRecord, key_registry, load_records
from paypal.client import PayPalClient
from paypal.config import (
    REPO_ROOT,
    Credentials,
    http_settings,
    load_env,
    load_yaml,
    merchant_credentials,
    operator_credentials,
    require_env,
)
from paypal.ledger import RequestLedger

Sink = Callable[[str, dict[str, Any]], None]


@dataclass
class Runtime:
    var: Path
    engine: Engine
    recorder: FlightRecorder
    ledger: RequestLedger
    records: dict[str, MerchantRecord]
    keys: MerchantKeyRegistry
    http: httpx.Client
    app_cfg: dict[str, Any]
    users: dict[str, Any]
    spec: dict[str, Any]
    router: LLMRouter = field(init=False)
    _sink: Sink = field(init=False)

    def __post_init__(self) -> None:
        self._sink = lambda _t, _p: None
        # LLM events go wherever the active session (purchase or recourse) is recording.
        self.router = build_router(self.engine, record=lambda t, p: self._sink(t, p))

    @classmethod
    def load(cls, var: Path | None = None) -> Runtime:
        load_env()
        var = var or REPO_ROOT / "var"
        engine = create_engine(f"sqlite:///{var / 'provenant.db'}")
        records = load_records(var / "registry.json")
        return cls(
            var=var,
            engine=engine,
            recorder=FlightRecorder(engine),
            ledger=RequestLedger.from_url(f"sqlite:///{var / 'ledger.db'}"),
            records=records,
            keys=key_registry(records),
            http=httpx.Client(timeout=30),
            app_cfg=load_yaml("app.yaml"),
            users=load_yaml("demo/users.yaml")["users"],
            spec=load_yaml("catalog/trail_running.yaml"),
        )

    # ---- pieces -------------------------------------------------------------------

    def record_into(self, sink: Sink) -> None:
        self._sink = sink

    @property
    def custom_id_format(self) -> CustomIdFormat:
        c = self.app_cfg["paypal"]["custom_id"]
        return CustomIdFormat(c["prefix"], int(c["hash_hex_chars"]), int(c["max_length"]))

    def paypal(self, merchant_id: str) -> PayPalClient:
        return PayPalClient(merchant_credentials(merchant_id), http_settings(), ledger=self.ledger)

    def operator(self) -> PayPalClient:
        return PayPalClient(operator_credentials(), http_settings(), ledger=self.ledger)

    def credentials(self, merchant_id: str) -> Credentials:
        return merchant_credentials(merchant_id)

    def vault(self, user_id: str) -> AddressVault:
        vault = AddressVault()
        for ref, addr in self.users[user_id]["addresses"].items():
            vault.save_confirmed(user_id, ref, Address(**addr))
        return vault

    def toolbox(self, sink: Sink, *, rank_prompt: str = "rank_v1") -> HttpToolbox:
        return HttpToolbox(
            merchants={k: r.base_url for k, r in self.records.items()},
            keys=self.keys,
            router=self.router,
            http=self.http,
            put_blob=self.recorder.put_blob,
            record=sink,
            seed=int(self.app_cfg["purchase"]["q_llm_seed"]),
            rank_prompt=rank_prompt,
        )

    # ---- sessions -----------------------------------------------------------------

    def new_session(
        self, user_id: str, *, rank_prompt: str = "rank_v1", session_id: str | None = None
    ) -> PurchaseSession:
        session_id = session_id or f"s-{uuid.uuid4().hex[:12]}"
        user_key = load_or_create(self.var / "keys" / "users", user_id)

        def sink(event_type: str, payload: dict[str, Any]) -> None:
            self.recorder.append(session_id, event_type, payload)

        self.record_into(sink)
        return PurchaseSession(
            session_id=session_id,
            recorder=self.recorder,
            router=self.router,
            toolbox=self.toolbox(sink, rank_prompt=rank_prompt),
            vault=self.vault(user_id),
            user_id=user_id,
            user_key=user_key,
            user_keys={key_id(user_key.public_key()): user_key.public_key()},
            nonces=SqlNonceRegistry(self.engine),
            custom_id_format=self.custom_id_format,
            paypal=self.paypal,
            settings=self.app_cfg["purchase"],
            category_spec=self.spec,
            now=lambda: datetime.now(UTC),
            experience_context={
                "user_action": "CONTINUE",
                "return_url": require_env("PAYPAL_RETURN_URL"),
                "cancel_url": require_env("PAYPAL_CANCEL_URL"),
            },
        )
