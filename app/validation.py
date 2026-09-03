"""Request parsing and validation.

Hand-rolled rather than a schema library: the stack rules out dependencies that
duplicate what a few dozen lines do (§37), and these helpers carry two
behaviours a generic validator would not give us for free — a MISSING sentinel
so PATCH can tell "field absent" from "field set to null", and strict rejection
of unknown fields so a typo fails loudly instead of being silently dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from flask import request

from app.errors import ValidationError


class _Missing:
    """Absent from the request body — distinct from an explicit null."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValidationError("Expected a JSON object in the request body.")
    return body


def reject_unknown_fields(body: dict[str, Any], allowed: Sequence[str]) -> None:
    """Fail on any field we do not handle.

    Two reasons. A typo (``stock_quantiy``) would otherwise be accepted and
    silently ignored, and a client cannot probe for writable attributes that
    were never meant to be part of the contract.
    """
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise ValidationError(
            f"Unknown field{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}."
        )


def body_str(
    body: dict[str, Any],
    field: str,
    *,
    required: bool = False,
    max_length: int = 255,
    nullable: bool = False,
) -> Any:
    if field not in body:
        if required:
            raise ValidationError(f"{field} is required.")
        return MISSING

    value = body[field]
    if value is None:
        if nullable:
            return None
        raise ValidationError(f"{field} cannot be null.")

    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text.")

    value = value.strip()
    if not value and required:
        raise ValidationError(f"{field} is required.")
    if len(value) > max_length:
        raise ValidationError(f"{field} must be {max_length} characters or fewer.")
    return value


def body_decimal(
    body: dict[str, Any],
    field: str,
    *,
    required: bool = False,
    nullable: bool = False,
    minimum: Decimal | None = None,
) -> Any:
    if field not in body:
        if required:
            raise ValidationError(f"{field} is required.")
        return MISSING

    value = body[field]
    if value is None:
        if nullable:
            return None
        raise ValidationError(f"{field} cannot be null.")

    # Accept a number or a decimal string; reject bools, which are ints in
    # Python and would otherwise sail through as 0 or 1.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError(f"{field} must be a number.")

    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValidationError(f"{field} must be a number.") from None

    if not parsed.is_finite():
        raise ValidationError(f"{field} must be a number.")
    if minimum is not None and parsed < minimum:
        raise ValidationError(f"{field} must be at least {minimum}.")
    if parsed.as_tuple().exponent < -2:
        raise ValidationError(f"{field} cannot have more than two decimal places.")
    return parsed


def body_int(
    body: dict[str, Any],
    field: str,
    *,
    required: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> Any:
    if field not in body:
        if required:
            raise ValidationError(f"{field} is required.")
        return MISSING

    value = body[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be a whole number.")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} must be at most {maximum}.")
    return value


def body_bool(body: dict[str, Any], field: str, *, required: bool = False) -> Any:
    if field not in body:
        if required:
            raise ValidationError(f"{field} is required.")
        return MISSING

    value = body[field]
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be true or false.")
    return value


def body_choice(
    body: dict[str, Any],
    field: str,
    allowed: Sequence[str],
    *,
    required: bool = False,
) -> Any:
    if field not in body:
        if required:
            raise ValidationError(f"{field} is required.")
        return MISSING

    value = body[field]
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{field} must be one of: {', '.join(allowed)}.")
    return value


def query_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    """Read an integer query parameter, or fail with a message that says why."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default

    try:
        value = int(raw)
    except ValueError:
        raise ValidationError(f"{name} must be a whole number.") from None

    if not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}.")
    return value


def query_decimal(name: str) -> Decimal | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None

    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValidationError(f"{name} must be a number.") from None

    if value < 0:
        raise ValidationError(f"{name} cannot be negative.")
    return value


def query_choice(name: str, allowed: Sequence[str], *, default: str) -> str:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default

    if raw not in allowed:
        raise ValidationError(
            f"{name} must be one of: {', '.join(allowed)}."
        )
    return raw


def query_bool(name: str) -> bool | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    raise ValidationError(f"{name} must be true or false.")


def query_str(name: str, *, max_length: int = 120) -> str | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) > max_length:
        raise ValidationError(f"{name} must be {max_length} characters or fewer.")
    return raw


def required_str(body: dict[str, Any], field: str, *, max_length: int = 255) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required.")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field} must be {max_length} characters or fewer.")
    return value
