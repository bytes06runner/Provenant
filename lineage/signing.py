"""Ed25519 signatures over JCS-canonical documents.

A signature covers `purpose || 0x00 || JCS(payload)`. The purpose string (for example
"provenant/mandate/v1") gives domain separation: a signature made for a manifest can never
verify as a mandate, even if the payload bytes happen to match.

The envelope also carries the payload's SHA-256. Verification recomputes it, so the hash that
other records refer to (custom_id, recorder events) is always the hash of what was signed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lineage.canonical import canonicalize

ALGORITHM = "Ed25519"


class SignatureError(Exception):
    """Any reason a signed envelope must not be trusted."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as e:
        raise SignatureError("malformed base64url") from e


def public_key_b64(key: Ed25519PublicKey) -> str:
    return b64url(key.public_bytes(Encoding.Raw, PublicFormat.Raw))


def load_public_key(text: str) -> Ed25519PublicKey:
    raw = b64url_decode(text)
    if len(raw) != 32:
        raise SignatureError("Ed25519 public keys are 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def key_id(key: Ed25519PublicKey) -> str:
    """Stable id: first 16 bytes of SHA-256 over the raw public key."""
    raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return b64url(hashlib.sha256(raw).digest()[:16])


def _message(purpose: str, payload: Any) -> bytes:
    if not purpose or "\x00" in purpose:
        raise SignatureError("purpose must be a non-empty string without NUL")
    return purpose.encode() + b"\x00" + canonicalize(payload)


@dataclass(frozen=True)
class SignedEnvelope:
    purpose: str
    payload: dict[str, Any]
    payload_hash: str  # hex SHA-256 of JCS(payload)
    key_id: str
    signature: str  # base64url
    alg: str = ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "key_id": self.key_id,
            "signature": self.signature,
            "alg": self.alg,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignedEnvelope:
        try:
            return cls(
                purpose=str(data["purpose"]),
                payload=dict(data["payload"]),
                payload_hash=str(data["payload_hash"]),
                key_id=str(data["key_id"]),
                signature=str(data["signature"]),
                alg=str(data.get("alg", ALGORITHM)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise SignatureError(f"malformed envelope: {e}") from e


def sign(purpose: str, payload: dict[str, Any], key: Ed25519PrivateKey) -> SignedEnvelope:
    message = _message(purpose, payload)
    return SignedEnvelope(
        purpose=purpose,
        payload=payload,
        payload_hash=hashlib.sha256(canonicalize(payload)).hexdigest(),
        key_id=key_id(key.public_key()),
        signature=b64url(key.sign(message)),
    )


def verify(
    envelope: SignedEnvelope, expected_purpose: str, trusted_keys: dict[str, Ed25519PublicKey]
) -> None:
    """Raise SignatureError unless the envelope is authentic for this purpose and key set."""
    if envelope.alg != ALGORITHM:
        raise SignatureError(f"unsupported algorithm {envelope.alg!r}")
    if envelope.purpose != expected_purpose:
        raise SignatureError(f"signed for {envelope.purpose!r}, expected {expected_purpose!r}")
    key = trusted_keys.get(envelope.key_id)
    if key is None:
        raise SignatureError(f"unknown key id {envelope.key_id!r}")
    if key_id(key) != envelope.key_id:
        raise SignatureError("key id does not match the registered key")
    if hashlib.sha256(canonicalize(envelope.payload)).hexdigest() != envelope.payload_hash:
        raise SignatureError("payload hash mismatch")
    try:
        key.verify(b64url_decode(envelope.signature), _message(expected_purpose, envelope.payload))
    except InvalidSignature as e:
        raise SignatureError("bad signature") from e
