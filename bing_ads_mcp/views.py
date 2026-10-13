"""Explicit presentation choices. Snapshots and source cells remain complete."""

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .client import ApiError

MISSING = object()


def field_value(row, field):
    if isinstance(row, dict) and field in row:
        return row[field]
    value = row
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def validate_fields(rows, fields):
    if rows:
        for field in fields:
            if not any(field_value(row, field) is not MISSING for row in rows):
                raise ApiError(f"Field {field} was not returned. Check the source field names.")


def project(row, fields):
    if fields is None or not isinstance(row, dict):
        return row
    output = {}
    for field in fields:
        value = field_value(row, field)
        if value is MISSING:
            continue
        parts = [field] if field in row else field.split(".")
        target = output
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = deepcopy(value)
    return output


def numeric(value):
    """JSON numbers where decimal round-trip is safe, otherwise exact decimal strings."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().removesuffix("%")
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
        if abs(number) > 2**53 - 1:
            return format(number, "f")
        if number == number.to_integral_value():
            return int(number)
        candidate = float(number)
        return candidate if Decimal(str(candidate)) == number else format(number, "f")
    except InvalidOperation, ValueError, OverflowError:
        return None


def numeric_view(row, fields):
    return {
        field: numeric(value)
        for field in fields
        if (value := field_value(row, field)) is not MISSING
    }
