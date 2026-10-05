"""Live hijack probes: try the attacks a poisoned storefront attempts, through the same contracts.

Used by the demo script and the buyer app's run view. Each probe builds the checkout an attack
would produce and hands it to `PurchaseSession.check_hijack`, which records the contract result
(and confirms the order builder refuses it) in the Flight Recorder.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from lineage.contracts import ContractResult
from lineage.interpreter import Proposal, Toolbox
from lineage.labels import untrusted
from lineage.mandate import VerifiedMandate
from lineage.purchase import PurchaseSession


def payee_swap(
    session: PurchaseSession,
    toolbox: Toolbox,
    vm: VerifiedMandate,
    proposal: Proposal,
    *,
    attacker_id: str,
    attacker_base_url: str,
) -> list[tuple[str, ContractResult, dict[str, Any]]]:
    """Two payee swaps: the payee an attacker's page injects (UNTRUSTED), and the attacker's own
    validly signed payee (signed, but by a merchant that is not the one being paid)."""
    c = proposal.candidate.checkout
    atk = toolbox.fetch_manifest(attacker_id)
    url = f"{attacker_base_url}/reviews/{atk.manifest.catalog[0].sku}"
    text, digest = toolbox.fetch_page(url)
    found = toolbox.extract("payment_instructions_v1", text) or {}
    out = []
    injected = found.get("payee") if isinstance(found, dict) else None
    if injected:
        hijacked = dataclasses.replace(
            c,
            payee=untrusted(injected, f"page:{url}", "qllm:payment_instructions_v1.payee", digest),
        )
        out.append(
            (
                "payee from injected page text",
                session.check_hijack(vm, proposal, hijacked, "payee from injected page text"),
                {"url": url, "extracted": found},
            )
        )
    signed = dataclasses.replace(c, payee=atk.payee())
    out.append(
        (
            "attacker's own signed payee",
            session.check_hijack(vm, proposal, signed, "attacker's own signed payee"),
            {"merchant": attacker_id},
        )
    )
    return out
