"""Redaction for anything PayPal-shaped that may be logged or written to disk."""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEYS = {"access_token", "client_secret", "authorization", "refresh_token", "id_token"}
_EMAIL_KEYS = {"email_address", "email", "payer_email", "receiver"}
_NAME_KEYS = {"given_name", "surname", "full_name"}
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+)")


def mask_email(value: str) -> str:
    """Keep the first character and the domain: j***@example.com."""
    return _EMAIL_RE.sub(r"\1***@\2", value)


def redact(obj: Any) -> Any:
    """Return a deep copy with tokens, secrets, payer emails and payer names masked."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k).lower()
            if key in _SECRET_KEYS:
                out[k] = "[REDACTED]"
            elif key in _EMAIL_KEYS and isinstance(v, str):
                out[k] = mask_email(v)
            elif key in _NAME_KEYS and isinstance(v, str):
                out[k] = (v[:1] + "***") if v else v
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return mask_email(obj)
    return obj
