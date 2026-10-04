"""RFC 8785 JSON Canonicalization Scheme (JCS) for everything we hash or sign.

Uses the `rfc8785` library for the canonical form, with one stricter rule of our own: floats are
refused anywhere in a signed document. JCS serializes floats deterministically, but money must
never pass through binary floating point, so amounts are decimal strings ("12.50").
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

import rfc8785


class CanonicalizationError(ValueError):
    pass


def _check(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, float):
        raise CanonicalizationError(f"float at {path or '$'}: use a decimal string")
    if isinstance(value, Decimal):
        raise CanonicalizationError(f"Decimal at {path or '$'}: serialize to a string first")
    if isinstance(value, int):
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"non-string key at {path or '$'}: {k!r}")
            _check(v, f"{path}.{k}")
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalizationError(f"unsupported type at {path or '$'}: {type(value).__name__}")


def canonicalize(document: Any) -> bytes:
    _check(document, "")
    try:
        return rfc8785.dumps(document)
    except (rfc8785.CanonicalizationError, ValueError, TypeError) as e:
        raise CanonicalizationError(str(e)) from e


def content_hash(document: Any) -> str:
    """Hex SHA-256 of the canonical form."""
    return hashlib.sha256(canonicalize(document)).hexdigest()
