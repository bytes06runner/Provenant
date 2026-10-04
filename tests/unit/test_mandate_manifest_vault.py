"""Mandate signing and verification, manifest verification, and the address vault."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lineage.labels import Label
from lineage.mandate import IntentMandate, MandateError, sign_mandate, verify_mandate
from lineage.manifest import (
    ManifestError,
    MerchantManifest,
    sign_manifest,
    verify_manifest,
)
from lineage.signing import SignedEnvelope, sign
from lineage.vault import VaultError

from .lineage_world import HOME, NOW, USER, World, mandate_payload, manifest_payload


@pytest.fixture
def world() -> World:
    return World()


# ---- Mandate -----------------------------------------------------------------


def test_verified_mandate_fields_are_user_labeled_and_sourced(world):
    vm = world.mandate()
    qty = vm.quantity()
    assert qty.value == 1 and qty.label is Label.USER
    assert qty.source_refs() == {f"mandate:{vm.envelope.payload_hash}"}
    assert vm.max_total().value == Decimal("130.00")


def test_mandate_signed_by_another_key_rejected(world):
    env = sign_mandate(IntentMandate(**mandate_payload()), Ed25519PrivateKey.generate())
    with pytest.raises(MandateError, match="signature"):
        verify_mandate(env, user_id=USER, user_keys=world.user_keys, now=NOW)


def test_mandate_for_another_user_rejected(world):
    env = sign_mandate(IntentMandate(**mandate_payload(user_id="user-b")), world.user_key)
    with pytest.raises(MandateError, match="different user"):
        verify_mandate(env, user_id=USER, user_keys=world.user_keys, now=NOW)


def test_tampered_mandate_rejected(world):
    env = sign_mandate(IntentMandate(**mandate_payload()), world.user_key)
    forged = SignedEnvelope(**{**env.__dict__, "payload": {**env.payload, "max_total": "999.00"}})
    with pytest.raises(MandateError, match="signature"):
        verify_mandate(forged, user_id=USER, user_keys=world.user_keys, now=NOW)


@pytest.mark.parametrize("delta", [timedelta(hours=1), timedelta(hours=2)])
def test_expired_mandate_rejected(world, delta):
    env = sign_mandate(IntentMandate(**mandate_payload()), world.user_key)
    with pytest.raises(MandateError, match="expired"):
        verify_mandate(env, user_id=USER, user_keys=world.user_keys, now=NOW + delta)


def test_not_yet_valid_mandate_rejected(world):
    env = sign_mandate(IntentMandate(**mandate_payload()), world.user_key)
    with pytest.raises(MandateError, match="not valid yet"):
        verify_mandate(env, user_id=USER, user_keys=world.user_keys, now=NOW - timedelta(hours=1))


def test_replayed_mandate_rejected(world):
    claimed: set[tuple[str, str]] = set()

    class Nonces:
        def claim(self, user_id: str, nonce: str) -> bool:
            if (user_id, nonce) in claimed:
                return False
            claimed.add((user_id, nonce))
            return True

    env = sign_mandate(IntentMandate(**mandate_payload()), world.user_key)
    verify_mandate(env, user_id=USER, user_keys=world.user_keys, now=NOW, nonces=Nonces())
    with pytest.raises(MandateError, match="replay"):
        verify_mandate(env, user_id=USER, user_keys=world.user_keys, now=NOW, nonces=Nonces())


def test_manifest_signature_cannot_pass_as_mandate(world):
    """Same key, same bytes, wrong purpose: domain separation must reject it."""
    payload = IntentMandate(**mandate_payload()).to_payload()
    env = sign("provenant/manifest/v1", payload, world.user_key)
    relabeled = SignedEnvelope(**{**env.__dict__, "purpose": "provenant/mandate/v1"})
    with pytest.raises(MandateError):
        verify_mandate(relabeled, user_id=USER, user_keys=world.user_keys, now=NOW)


def test_extra_fields_in_signed_mandate_rejected(world):
    payload = {**IntentMandate(**mandate_payload()).to_payload(), "payee_override": "ATK"}
    env = sign("provenant/mandate/v1", payload, world.user_key)
    with pytest.raises(MandateError, match="invalid mandate"):
        verify_mandate(env, user_id=USER, user_keys=world.user_keys, now=NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_total": "12.5.0"},
        {"max_total": "-1.00"},
        {"max_unit_price": "1e3"},
        {"max_total": 120.0},
        {"currency": "usd"},
        {"quantity": 0},
        {"max_unit_price": "200.00", "max_total": "100.00"},
        {"expires_at": NOW - timedelta(days=1)},
        {"issued_at": NOW.replace(tzinfo=None)},
        {"required_attributes": {"material": "leather"}},
        {"nonce": "short"},
    ],
)
def test_invalid_mandates_cannot_be_constructed(overrides):
    with pytest.raises(ValueError):
        IntentMandate(**mandate_payload(**overrides))


def test_mandate_payload_is_stable_across_timezones(world):
    a = IntentMandate(**mandate_payload())
    b = IntentMandate(
        **mandate_payload(
            issued_at=a.issued_at.astimezone(tz=None), expires_at=a.expires_at.astimezone(tz=None)
        )
    )
    assert a.to_payload() == b.to_payload()


# ---- Manifest ----------------------------------------------------------------


def test_verified_manifest_fields_are_merchant_signed_with_manifest_hash(world):
    vm = world.manifest()
    price = vm.unit_price("TRAIL-BLK-10")
    assert price.value == Decimal("99.00") and price.label is Label.MERCHANT_SIGNED
    (src,) = price.sources
    assert src.ref == f"manifest:northwind:{vm.hash}" and src.digest == vm.hash
    assert src.path == "catalog[TRAIL-BLK-10].price"


def test_attacker_cannot_sign_for_honest_merchant(world):
    m = MerchantManifest(**manifest_payload("northwind", "ATKPAYEE666"))
    env = sign_manifest(m, world.attacker_key)
    with pytest.raises(ManifestError, match="signature"):
        verify_manifest(env, merchant_id="northwind", registry=world.registry)


def test_manifest_naming_a_different_merchant_rejected(world):
    """Attacker signs a manifest claiming to be northwind with its own registered key."""
    m = MerchantManifest(**manifest_payload("northwind", "ATKPAYEE666"))
    env = sign_manifest(m, world.attacker_key)
    with pytest.raises(ManifestError, match="different merchant"):
        verify_manifest(env, merchant_id="attacker", registry=world.registry)


def test_tampered_manifest_price_rejected(world):
    env = sign_manifest(
        MerchantManifest(**manifest_payload("northwind", "NWPAYEE123")), world.honest_key
    )
    catalog = [dict(p) for p in env.payload["catalog"]]
    catalog[0]["price"] = "9.00"
    forged = SignedEnvelope(**{**env.__dict__, "payload": {**env.payload, "catalog": catalog}})
    with pytest.raises(ManifestError, match="signature"):
        verify_manifest(forged, merchant_id="northwind", registry=world.registry)


def test_unregistered_merchant_rejected(world):
    env = sign_manifest(
        MerchantManifest(**manifest_payload("northwind", "NWPAYEE123")), world.honest_key
    )
    with pytest.raises(ManifestError, match="no registered keys"):
        verify_manifest(env, merchant_id="kestrel", registry=world.registry)


def test_unknown_sku(world):
    with pytest.raises(ManifestError, match="not in"):
        world.manifest().unit_price("NOPE")


@pytest.mark.parametrize(
    "overrides",
    [
        {"catalog": []},
        {"currency": "EUR"},
        {"tax_rate": "0.1.0"},
        {
            "policy": {
                "return_window_days": 30,
                "refund_caps": {
                    "fulfillment_fault": "1.50",
                    "misrepresentation": "1",
                    "decision_fault": "0",
                },
            }
        },
    ],
)
def test_invalid_manifests_cannot_be_constructed(overrides):
    with pytest.raises(ValueError):
        MerchantManifest(**manifest_payload("northwind", "NWPAYEE123", **overrides))


def test_duplicate_sku_rejected():
    payload = manifest_payload("northwind", "NWPAYEE123")
    payload["catalog"].append(dict(payload["catalog"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        MerchantManifest(**payload)


# ---- Vault -------------------------------------------------------------------


def test_vault_address_is_user_labeled_and_sourced_to_entry(world):
    addr = world.vault.lookup(USER, "home")
    assert addr.value == HOME and addr.label is Label.USER
    assert addr.source_refs() == {"vault:user-a:home"}


def test_vault_is_per_user(world):
    with pytest.raises(VaultError):
        world.vault.lookup("user-b", "home")
