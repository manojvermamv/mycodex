"""Small boundary checks with field-only errors, never input values or credentials."""

from __future__ import annotations

import math
from typing import Any


class InvalidData(ValueError):
    pass


def object_value(value: Any, field: str, optional: bool = False) -> dict[str, Any]:
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise InvalidData(f"{field} must contain named fields (a JSON object)")
    return value


def text_value(value: Any, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidData(f"{field} must be non-empty text")
    return value


def boolean(value: Any, field: str, optional: bool = False) -> bool | None:
    if value is None and optional:
        return None
    if not isinstance(value, bool):
        raise InvalidData(f"{field} must be true or false")
    return value


def number(value: Any, field: str, minimum: float = 0, maximum: float | None = None,
           integer: bool = False, optional: bool = False) -> int | float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidData(f"{field} must be a number")
    try:
        valid = math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
        valid = valid and (not integer or int(value) == value)
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise InvalidData(f"{field} must be a finite {'whole ' if integer else ''}number"
                          f" from {minimum:g}" + (f" to {maximum:g}" if maximum is not None else " or higher"))
    return int(value) if integer else value


def timestamp(value: Any, field: str, optional: bool = True) -> int | None:
    # Keep epochs representable by display helpers as well as JSON transport.
    return number(value, field, maximum=253402214400, integer=True, optional=optional)


def text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise InvalidData(f"{field} must be a list of account names")
    for item in value:
        text_value(item, field)
    return value
