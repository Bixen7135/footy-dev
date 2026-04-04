from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any, Optional

SHOE_SIZE_MIN = Decimal("34")
SHOE_SIZE_MAX = Decimal("50")
_SHOE_SIZE_TOKEN_RE = re.compile(r"^(?P<size>\d{2}(?:[.,]5)?)(?P<qty>\d+)?$")


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace(",", ".")
    try:
        parsed = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _normalize_plain_shoe_size_label(value: Any) -> Optional[str]:
    parsed = _to_decimal(value)
    if parsed is None:
        return None
    if parsed < SHOE_SIZE_MIN or parsed > SHOE_SIZE_MAX:
        return None

    doubled = parsed * 2
    if doubled != doubled.to_integral_value():
        return None

    if parsed == parsed.to_integral_value():
        return str(int(parsed))
    return str(parsed.normalize())


def decode_shoe_size_with_quantity(value: Any) -> tuple[Optional[str], Optional[int]]:
    if value is None:
        return None, None

    text = str(value).strip()
    if not text:
        return None, None

    token_match = _SHOE_SIZE_TOKEN_RE.fullmatch(text)
    if token_match:
        size_label = _normalize_plain_shoe_size_label(token_match.group("size"))
        if not size_label:
            return None, None
        qty_raw = token_match.group("qty")
        if qty_raw:
            return size_label, max(int(qty_raw), 0)
        return size_label, None

    return _normalize_plain_shoe_size_label(text), None


def normalize_shoe_size_label(value: Any) -> Optional[str]:
    size_label, _ = decode_shoe_size_with_quantity(value)
    return size_label


def normalize_shoe_size_key(value: Any) -> Optional[tuple[str, str]]:
    label = normalize_shoe_size_label(value)
    if not label:
        return None
    return (label.lower(), label)
