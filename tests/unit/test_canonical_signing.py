"""JCS canonicalization and Ed25519 envelopes."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given
from hypothesis import strategies as st

from lineage.canonical import CanonicalizationError, canonicalize, content_hash
from lineage.signing import (
    SignatureError,
    SignedEnvelope,
    b64url,
    key_id,
    load_public_key,
    public_key_b64,
    sign,
    verify,
)

# ---- JCS -------------------------------------------------------------------


def test_rfc8785_key_ordering_and_whitespace():
    # Keys sort by UTF-16 code units; no insignificant whitespace.
    doc = {"b": 1, "a": [True, None, "x"], "\u20ac": "euro", "\r": "cr", "1": 1}
    assert canonicalize(doc) == (
        b'{"\\r":"cr","1":1,"a":[true,null,"x"],"b":1,"\xe2\x82\xac":"euro"}'
    )


def test_rfc8785_string_escaping():
    assert (
        canonicalize({"s": '\u0000\u001f"\\/\u00e9'}) == b'{"s":"\\u0000\\u001f\\"\\\\/\xc3\xa9"}'
    )


json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**53 - 1), 2**53 - 1) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=20,
)


@given(json_values)
def test_canonical_form_ignores_insertion_order(doc):
    if isinstance(doc, dict):
        reordered = dict(reversed(list(doc.items())))
        assert canonicalize(reordered) == canonicalize(doc)
    assert content_hash(doc) == content_hash(copy.deepcopy(doc))


@pytest.mark.parametrize("bad", [{"price": 12.5}, {"a": [1, 2.0]}, {"p": Decimal("1.0")}])
def test_floats_and_decimals_are_refused(bad):
    with pytest.raises(CanonicalizationError):
        canonicalize(bad)


@pytest.mark.parametrize("n", [2**53, -(2**53), 10**30])
def test_integers_outside_the_exact_json_range_are_refused(n):
    with pytest.raises(CanonicalizationError):
        canonicalize({"n": n})


@pytest.mark.parametrize("bad", [{1: "x"}, {"s": {1, 2}}, {"b": b"bytes"}])
def test_non_json_types_are_refused(bad):
    with pytest.raises(CanonicalizationError):
        canonicalize(bad)


# ---- Ed25519 envelopes -------------------------------------------------------

PURPOSE = "provenant/mandate/v1"
PAYLOAD = {"category": "shoes", "max_total": "120.00", "quantity": 1}


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def keys(key):
    return {key_id(key.public_key()): key.public_key()}


def test_sign_and_verify_roundtrip(key, keys):
    env = sign(PURPOSE, PAYLOAD, key)
    verify(env, PURPOSE, keys)
    assert env.payload_hash == content_hash(PAYLOAD)


def test_serialization_roundtrip(key, keys):
    env = SignedEnvelope.from_dict(sign(PURPOSE, PAYLOAD, key).to_dict())
    verify(env, PURPOSE, keys)


def test_key_order_of_payload_does_not_matter(key, keys):
    env = sign(PURPOSE, PAYLOAD, key)
    reordered = SignedEnvelope(**{**env.__dict__, "payload": dict(reversed(PAYLOAD.items()))})
    verify(reordered, PURPOSE, keys)


def test_tampered_payload_fails_even_with_updated_hash(key, keys):
    env = sign(PURPOSE, PAYLOAD, key)
    evil = {**PAYLOAD, "max_total": "9999.00"}
    forged = SignedEnvelope(**{**env.__dict__, "payload": evil, "payload_hash": content_hash(evil)})
    with pytest.raises(SignatureError, match="bad signature"):
        verify(forged, PURPOSE, keys)


def test_tampered_payload_with_stale_hash_fails(key, keys):
    env = sign(PURPOSE, PAYLOAD, key)
    forged = SignedEnvelope(**{**env.__dict__, "payload": {**PAYLOAD, "quantity": 5}})
    with pytest.raises(SignatureError, match="hash mismatch"):
        verify(forged, PURPOSE, keys)


def test_wrong_key_fails(key):
    other = Ed25519PrivateKey.generate().public_key()
    env = sign(PURPOSE, PAYLOAD, key)
    with pytest.raises(SignatureError, match="unknown key id"):
        verify(env, PURPOSE, {key_id(other): other})


def test_key_id_swap_to_a_trusted_key_fails(key):
    """Attacker signs with their key but claims a trusted key's id."""
    trusted = Ed25519PrivateKey.generate().public_key()
    env = sign(PURPOSE, PAYLOAD, key)
    forged = SignedEnvelope(**{**env.__dict__, "key_id": key_id(trusted)})
    with pytest.raises(SignatureError, match="bad signature"):
        verify(forged, PURPOSE, {key_id(trusted): trusted})


def test_registry_entry_must_match_its_key_id(key):
    other = Ed25519PrivateKey.generate().public_key()
    env = sign(PURPOSE, PAYLOAD, key)
    with pytest.raises(SignatureError, match="does not match"):
        verify(env, PURPOSE, {env.key_id: other})


def test_domain_separation_between_purposes(key, keys):
    manifest_sig = sign("provenant/manifest/v1", PAYLOAD, key)
    relabeled = SignedEnvelope(**{**manifest_sig.__dict__, "purpose": PURPOSE})
    with pytest.raises(SignatureError, match="bad signature"):
        verify(relabeled, PURPOSE, keys)
    with pytest.raises(SignatureError, match="expected"):
        verify(manifest_sig, PURPOSE, keys)


def test_unsupported_algorithm_rejected(key, keys):
    env = SignedEnvelope(**{**sign(PURPOSE, PAYLOAD, key).__dict__, "alg": "none"})
    with pytest.raises(SignatureError, match="algorithm"):
        verify(env, PURPOSE, keys)


@pytest.mark.parametrize("sig", ["", "!!!", b64url(b"\x00" * 64)])
def test_garbage_signature_rejected(key, keys, sig):
    env = SignedEnvelope(**{**sign(PURPOSE, PAYLOAD, key).__dict__, "signature": sig})
    with pytest.raises(SignatureError):
        verify(env, PURPOSE, keys)


def test_public_key_text_roundtrip(key):
    pub = key.public_key()
    assert key_id(load_public_key(public_key_b64(pub))) == key_id(pub)
    with pytest.raises(SignatureError):
        load_public_key(b64url(b"short"))


def test_malformed_envelope_dict():
    with pytest.raises(SignatureError):
        SignedEnvelope.from_dict({"purpose": PURPOSE})


def test_purpose_with_nul_rejected(key):
    with pytest.raises(SignatureError):
        sign("a\x00b", PAYLOAD, key)


@pytest.mark.parametrize("text", ["a", "abcde"])
def test_malformed_base64_key_rejected(text):
    with pytest.raises(SignatureError):
        load_public_key(text)


def test_money_helpers():
    from lineage.money import MoneyError, format_amount, parse_amount, parse_currency

    assert format_amount(Decimal("5")) == "5.00"
    assert format_amount(parse_amount("12.5")) == "12.50"
    for bad in ["", "01.00", "1.234", "+1", " 1"]:
        with pytest.raises(MoneyError):
            parse_amount(bad)
    with pytest.raises(MoneyError):
        parse_currency("US")


@pytest.mark.parametrize("ok", ["0", "0.0825", "0.5", "1", "1.000000", "0.000001"])
def test_fraction_accepts_rates(ok):
    from lineage.money import parse_fraction

    assert Decimal("0") <= parse_fraction(ok) <= Decimal("1")


@pytest.mark.parametrize("bad", ["1.5", "1.01", "-0.1", "0.1234567", ".5", "", "2"])
def test_fraction_rejects_out_of_range(bad):
    from lineage.money import MoneyError, parse_fraction

    with pytest.raises(MoneyError):
        parse_fraction(bad)
