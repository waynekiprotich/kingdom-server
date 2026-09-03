"""Audit trail for admin changes (spec §19).

Every mutation an admin makes records who did it, to what, and the before and
after values. The trail outlives the account: ``admin_id`` is set null if the
admin is removed, but ``admin_email`` is copied in so the row still says who.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from flask import request
from flask_jwt_extended import get_jwt, get_jwt_identity

from app.extensions import db
from app.models.audit import AuditLog
from app.validation import MISSING


def _encode(value: Any) -> Any:
    """JSON-safe rendering of a column value."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def apply_changes(instance: Any, updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Assign every non-MISSING value, returning what actually changed.

    Doing the assignment and the diff in one place means the audit record
    cannot drift from what was really written — the same comparison decides
    both.
    """
    changes: dict[str, dict[str, Any]] = {}

    for field, value in updates.items():
        if value is MISSING:
            continue

        current = getattr(instance, field)
        if current == value:
            continue

        changes[field] = {"from": _encode(current), "to": _encode(value)}
        setattr(instance, field, value)

    return changes


def record(
    action: str,
    entity_type: str,
    entity_id: Any,
    *,
    changes: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the current transaction.

    Not committed here: it is flushed with the change it describes, so an
    audit entry can never survive a rolled-back edit.
    """
    identity = get_jwt_identity()
    claims = get_jwt()

    db.session.add(
        AuditLog(
            admin_id=int(identity) if identity is not None else None,
            admin_email=claims.get("email"),
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            previous_value=json.dumps(
                {field: change["from"] for field, change in (changes or {}).items()}
            )
            if changes
            else None,
            new_value=json.dumps(
                {field: change["to"] for field, change in (changes or {}).items()}
            )
            if changes
            else None,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        )
    )
