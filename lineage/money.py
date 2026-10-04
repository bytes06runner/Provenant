"""Money parsing shared by signed documents: decimal strings in, Decimal out, never floats."""

from __future__ import annotations

import re
from decimal import Decimal

_AMOUNT = re.compile(r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class MoneyError(ValueError):
    pass


def parse_amount(text: str) -> Decimal:
    """Non-negative amount with at most two decimals, as PayPal accepts it ("12.50")."""
    if not isinstance(text, str) or not _AMOUNT.match(text):
        raise MoneyError(f"invalid amount {text!r}: expected a decimal string like '12.50'")
    return Decimal(text)


def format_amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def parse_currency(text: str) -> str:
    if not isinstance(text, str) or not _CURRENCY.match(text):
        raise MoneyError(f"invalid currency {text!r}: expected ISO 4217 like 'USD'")
    return text
