"""User vault: shipping addresses the user entered and confirmed in the Buyer App.

The agent never types an address. The mandate names one by reference (`ship_to_ref`), and the
vault returns it USER-labeled, sourced to that exact vault entry. An address scraped from a page
or produced by an LLM can only ever be UNTRUSTED.

Storage is in memory for now; the Postgres-backed store lands with the API in Phase 1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lineage.canonical import content_hash
from lineage.labels import MINT, Labeled, mint_user


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
