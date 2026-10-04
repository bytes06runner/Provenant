"""Provenance labels: every runtime value carries where it came from.

Lattice, most to least trusted:  USER > MERCHANT_SIGNED > DERIVED > UNTRUSTED

Rules (CLAUDE.md section 4.3):
  * An operation over labeled values yields the join (least trusted) of its inputs, and its
    sources are the union of theirs.
  * A computed value is never more trusted than DERIVED. So price x quantity, with inputs
    MERCHANT_SIGNED and USER, is DERIVED with both sources attached. This is what lets the
    `amount.total` contract demand "DERIVED, and only from signed and user sources".
  * Labels can only be raised by signature verification (to MERCHANT_SIGNED) or user
    confirmation (to USER). Both go through `mint_user` / `mint_merchant_signed`, which require
    a capability object only the mandate and manifest verifiers hold. Anything parsed from an
    LLM or a web page goes through `untrusted()`.

The capability check stops accidental upgrades (a stray constructor call on LLM output). It is
not a boundary against malicious code running in the same process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Label(IntEnum):
    """Higher value = more trusted. join() is min()."""

    UNTRUSTED = 0
    DERIVED = 1
    MERCHANT_SIGNED = 2
    USER = 3


def join(*labels: Label) -> Label:
    """Least trusted of the inputs. Joining nothing is an error, not a silent default."""
    if not labels:
        raise ValueError("join() needs at least one label")
    return min(labels)


@dataclass(frozen=True, order=True)
class Source:
    """One origin of a value.

    kind      the label this origin confers
    ref       stable reference: "mandate:<hash>", "manifest:<merchant_id>:<hash>",
              "page:<url>", "vault:<user>:<address_ref>"
    path      where inside that origin, e.g. "max_total" or "catalog[SKU-1].price"
    digest    content hash of the origin where one exists (manifest hash, page hash)
    """

    kind: Label
    ref: str
    path: str = ""
    digest: str = ""

    def describe(self) -> str:
        where = f"#{self.path}" if self.path else ""
        return f"{self.kind.name}:{self.ref}{where}"


class _MintCapability:
    """Held only by modules allowed to raise trust (mandate and manifest verification).

    Exactly one instance can ever exist. A second construction raises, so code cannot obtain
    the capability by instantiating the class instead of importing MINT (which a test polices).
    """

    _created = False

    def __new__(cls) -> _MintCapability:
        if cls._created:
            raise PermissionError("the mint capability cannot be created again")
        cls._created = True
        return super().__new__(cls)


MINT = _MintCapability()
_TRUSTED = (Label.USER, Label.MERCHANT_SIGNED)


@dataclass(frozen=True)
class Labeled[T]:
    value: T
    label: Label
    sources: frozenset[Source] = field(default_factory=frozenset)
    _capability: _MintCapability | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.label in _TRUSTED and self._capability is not MINT:
            raise PermissionError(
                f"{self.label.name} can only be minted by signature verification or user "
                "confirmation"
            )
        if not self.sources:
            raise ValueError("a labeled value must name at least one source")
        if any(s.kind < self.label for s in self.sources):
            raise ValueError("a value cannot be more trusted than its least trusted source")

    def source_refs(self) -> set[str]:
        return {s.ref for s in self.sources}

    def provenance(self) -> list[str]:
        return sorted(s.describe() for s in self.sources)


def untrusted[T](value: T, ref: str, path: str = "", digest: str = "") -> Labeled[T]:
    """Anything from a web page, a review, or an LLM's output."""
    return Labeled(value, Label.UNTRUSTED, frozenset({Source(Label.UNTRUSTED, ref, path, digest)}))


def mint_user[T](
    capability: _MintCapability, value: T, ref: str, path: str = "", digest: str = ""
) -> Labeled[T]:
    return Labeled(
        value, Label.USER, frozenset({Source(Label.USER, ref, path, digest)}), capability
    )


def mint_merchant_signed[T](
    capability: _MintCapability, value: T, ref: str, path: str = "", digest: str = ""
) -> Labeled[T]:
    return Labeled(
        value,
        Label.MERCHANT_SIGNED,
        frozenset({Source(Label.MERCHANT_SIGNED, ref, path, digest)}),
        capability,
    )


def derive[R](fn: Callable[..., R], *inputs: Labeled[Any]) -> Labeled[R]:
    """Apply a deterministic function to labeled inputs.

    Result label: join(DERIVED, *input labels). Result sources: union of input sources.
    """
    if not inputs:
        raise ValueError("derive() needs at least one labeled input")
    label = join(Label.DERIVED, *(i.label for i in inputs))
    sources = frozenset().union(*(i.sources for i in inputs))
    return Labeled(fn(*(i.value for i in inputs)), label, sources)
