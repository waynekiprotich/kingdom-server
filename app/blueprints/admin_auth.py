"""Admin authentication (spec §13, §15).

Customers never authenticate. These endpoints exist only for the admin
dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)
import logging

from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from app.authz import ACTOR_ADMIN, actor_claims, require_actor
from app.errors import ApiError, AuthenticationError, PermissionError_
from app.extensions import db
from app.models.admin import Admin
from app.models.token import TokenBlocklist
from app.services import audit, rate_limit
from app.validation import json_body, required_str

logger = logging.getLogger(__name__)

bp = Blueprint("admin_auth", __name__, url_prefix="/api/admin/auth")

#: Compared against when no account matches, so a wrong email and a wrong
#: password take the same amount of time (§16 — no user enumeration).
_DUMMY_HASH = generate_password_hash("not-a-real-password")


class AccountLockedError(ApiError):
    status_code = 429
    code = "ACCOUNT_LOCKED"
    message = "Too many failed attempts. Try again shortly."


def _admin_claims(admin: Admin) -> dict[str, str]:
    # `actor` is what stops a customer's token — same signing key, same numeric
    # identity shape — from being accepted here. See app/authz.py.
    return {
        **actor_claims(ACTOR_ADMIN),
        "role": str(admin.role),
        "email": admin.email,
    }


def _serialise(admin: Admin) -> dict[str, object]:
    return {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name,
        "role": str(admin.role),
    }


@bp.post("/login")
@rate_limit.limit("login")
def login():
    body = json_body()
    email = required_str(body, "email").lower()
    password = required_str(body, "password", max_length=256)

    admin = db.session.scalar(select(Admin).where(Admin.email == email))

    # Every refusal below is written to the audit trail and committed before
    # raising, because raising rolls nothing forward: an attack on the admin
    # sign-in that leaves no trace is the one worth recording most.
    if admin is None:
        # Burn the same time as a real check before failing.
        check_password_hash(_DUMMY_HASH, password)
        audit.record_auth("auth.login_failed", detail={"reason": "unknown_account"})
        db.session.commit()
        logger.warning("Admin sign-in failed: unknown account.")
        raise AuthenticationError()

    if admin.is_locked:
        audit.record_auth(
            "auth.login_locked", admin_id=admin.id, admin_email=admin.email
        )
        db.session.commit()
        logger.warning("Admin sign-in refused: account %s is locked.", admin.id)
        raise AccountLockedError()

    # The password is checked before the account's state is revealed. Saying
    # "deactivated" to someone who does not know the password would confirm
    # that the address belongs to an admin.
    if not admin.check_password(password):
        admin.register_failed_login(
            current_app.config["MAX_FAILED_LOGINS"],
            current_app.config["LOGIN_LOCKOUT_MINUTES"],
        )
        audit.record_auth(
            "auth.login_failed",
            admin_id=admin.id,
            admin_email=admin.email,
            detail={"reason": "wrong_password", "locked": admin.is_locked},
        )
        db.session.commit()
        logger.warning("Admin sign-in failed: wrong password for account %s.", admin.id)
        raise AuthenticationError()

    if not admin.is_active:
        audit.record_auth(
            "auth.login_failed",
            admin_id=admin.id,
            admin_email=admin.email,
            detail={"reason": "deactivated"},
        )
        db.session.commit()
        raise PermissionError_("This account has been deactivated.")

    admin.register_successful_login()
    audit.record_auth("auth.login", admin_id=admin.id, admin_email=admin.email)
    db.session.commit()

    identity = str(admin.id)
    claims = _admin_claims(admin)

    return jsonify(
        {
            "success": True,
            "data": {
                "admin": _serialise(admin),
                "access_token": create_access_token(identity, additional_claims=claims),
                "refresh_token": create_refresh_token(identity, additional_claims=claims),
            },
        }
    )


@bp.post("/refresh")
@rate_limit.limit("refresh")
@jwt_required(refresh=True)
def refresh():
    # Refresh is the sharpest edge of the actor rule: without this check a
    # *customer's* refresh token would mint an admin access token for whichever
    # admin happens to share its numeric id.
    require_actor(ACTOR_ADMIN)

    admin = db.session.get(Admin, int(get_jwt_identity()))
    if admin is None or not admin.is_active:
        raise PermissionError_("This account is no longer active.")

    return jsonify(
        {
            "success": True,
            "data": {
                "access_token": create_access_token(
                    str(admin.id), additional_claims=_admin_claims(admin)
                )
            },
        }
    )


@bp.post("/logout")
@jwt_required(verify_type=False)
def logout():
    """Revoke the presented token.

    Called once with the access token and once with the refresh token; the
    client discards both either way.
    """
    require_actor(ACTOR_ADMIN)

    token = get_jwt()
    db.session.add(
        TokenBlocklist(
            jti=token["jti"],
            token_type=token["type"],
            admin_id=int(token["sub"]),
            expires_at=datetime.fromtimestamp(token["exp"], tz=timezone.utc),
        )
    )
    audit.record_auth(
        "auth.logout",
        admin_id=int(token["sub"]),
        admin_email=token.get("email"),
        detail={"token_type": token["type"]},
    )
    db.session.commit()
    return jsonify({"success": True, "data": {"revoked": token["type"]}})


@bp.get("/me")
@jwt_required()
def me():
    require_actor(ACTOR_ADMIN)
    admin = db.session.get(Admin, int(get_jwt_identity()))
    if admin is None or not admin.is_active:
        raise PermissionError_("This account is no longer active.")
    return jsonify({"success": True, "data": {"admin": _serialise(admin)}})
