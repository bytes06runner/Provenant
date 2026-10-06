"""User vault: shipping addresses the user entered and confirmed in the Buyer App.

The agent never types an address. The mandate names one by reference (`ship_to_ref`), and the
vault returns it USER-labeled, sourced to that exact vault entry. An address scraped from a page
or produced by an LLM can only ever be UNTRUSTED.

Two stores with one interface: `AddressVault` (in memory, tests and scripts) and
`SqlAddressVault` (a table, Postgres in deployment). Both label what they return the same way.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, delete, insert, select
from sqlalchemy.engine import Engine

from lineage.canonical import content_hash
from lineage.labels import MINT, Labeled, mint_user
from paypal.storage import ensure_schema

metadata = MetaData()

vault_addresses = Table(
    "vault_addresses",
    metadata,
    Column("user_id", String(64), primary_key=True),
    Column("ref", String(64), primary_key=True),
    Column("address_json", Text, nullable=False),
    Column("confirmed_at", DateTime(timezone=True), nullable=False),
)


class Address(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    full_name: str = Field(min_length=1, max_length=300)
    address_line_1: str = Field(min_length=1, max_length=300)
    address_line_2: str = ""
    admin_area_2: str = Field(min_length=1, max_length=120)  # city
    admin_area_1: str = ""  # state / province
    postal_code: str = ""
    country_code: str = Field(pattern=r"^[A-Z]{2}$")


class VaultError(KeyError):
    pass


class AddressVault:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], Address] = {}

    def save_confirmed(self, user_id: str, ref: str, address: Address) -> None:
        """Called only from the user-facing confirmation flow."""
        self._entries[(user_id, ref)] = address

    @staticmethod
    def source_ref(user_id: str, ref: str) -> str:
        return f"vault:{user_id}:{ref}"

    def lookup(self, user_id: str, ref: str) -> Labeled[Address]:
        try:
            address = self._entries[(user_id, ref)]
        except KeyError:
            raise VaultError(f"no confirmed address {ref!r} for this user") from None
        return mint_user(
            MINT,
            address,
            self.source_ref(user_id, ref),
            digest=content_hash(address.model_dump(mode="json")),
        )


class SqlAddressVault(AddressVault):
    """The same vault, persisted: confirmed addresses survive restarts and deploys."""

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        ensure_schema(metadata, engine)  # SQLite only; Alembic owns Postgres

    def save_confirmed(self, user_id: str, ref: str, address: Address) -> None:
        row = {
            "user_id": user_id,
            "ref": ref,
            "address_json": json.dumps(address.model_dump(mode="json"), sort_keys=True),
            "confirmed_at": datetime.now(UTC),
        }
        with self.engine.begin() as conn:
            conn.execute(
                delete(vault_addresses).where(
                    vault_addresses.c.user_id == user_id, vault_addresses.c.ref == ref
                )
            )
            conn.execute(insert(vault_addresses).values(**row))

    def lookup(self, user_id: str, ref: str) -> Labeled[Address]:
        with self.engine.connect() as conn:
            raw = conn.execute(
                select(vault_addresses.c.address_json).where(
                    vault_addresses.c.user_id == user_id, vault_addresses.c.ref == ref
                )
            ).scalar_one_or_none()
        if raw is None:
            raise VaultError(f"no confirmed address {ref!r} for this user")
        self._entries[(user_id, ref)] = Address.model_validate(json.loads(raw))
        return super().lookup(user_id, ref)
