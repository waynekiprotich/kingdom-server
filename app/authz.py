"""Admin route guards (spec §15).

One decorator does all four checks an admin endpoint needs: a valid token, an
account that still exists, an account that is still active, and a sufficient
role. Doing them separately is how one gets forgotten.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import wraps

from flask import g
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.errors import PermissionError_
from app.extensions import db
from app.models.admin import Admin, AdminRole


def admin_required(*roles: AdminRole):
    """Require a signed-in, active admin — optionally of a specific role.

    Called with no roles, any admin passes. A deactivated admin is refused even
    while holding an unexpired token, so revoking access does not wait for the
    token to lapse.
    """
    allowed: Sequence[str] = [role.value for role in roles]

    def decorator(view):
        @wraps(view)
        @jwt_required()
        def wrapper(*args, **kwargs):
            admin = db.session.get(Admin, int(get_jwt_identity()))

            if admin is None or not admin.is_active:
                raise PermissionError_("This account is no longer active.")

            if allowed and str(admin.role) not in allowed:
                raise PermissionError_(
                    "Your role does not permit that action.",
                    code="INSUFFICIENT_ROLE",
                )

            g.admin = admin
            return view(*args, **kwargs)

        return wrapper

    return decorator
