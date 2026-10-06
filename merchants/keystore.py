"""Merchant signing keys for the storefront simulator.

Each simulated merchant has one Ed25519 key, created on first use and kept under `var/keys/`
(gitignored, mode 0600). Only the merchant side (the simulator) reads private keys. Provenant
learns the public key at onboarding (scripts/register_merchants.py) and never sees the private
one.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)


class KeystoreError(RuntimeError):
    pass


def keys_dir(default: Path) -> Path:
    return Path(os.environ.get("PROVENANT_KEYS_DIR") or default)


def user_keys_dir(default: Path) -> Path:
    return Path(os.environ.get("PROVENANT_USER_KEYS_DIR") or default)


def registry_path(default: Path) -> Path:
    return Path(os.environ.get("PROVENANT_REGISTRY_PATH") or default)


def read_only() -> bool:
    """Deployed services load keys from secret files and must never mint new ones."""
    return os.environ.get("PROVENANT_KEYS_READONLY") == "1"


def load_or_create(keys_dir: Path, merchant_key: str) -> Ed25519PrivateKey:
    if not merchant_key.isidentifier():
        raise KeystoreError(f"invalid merchant key {merchant_key!r}")
    path = keys_dir / f"{merchant_key}.ed25519.pem"
    if path.exists():
        key = load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise KeystoreError(f"{path} is not an Ed25519 key")
        return key
    if read_only():
        raise KeystoreError(f"no key for {merchant_key!r} in {keys_dir} (keys are read-only here)")
    keys_dir.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key
