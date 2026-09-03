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
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app.errors import ApiError, AuthenticationError, PermissionError_
from app.extensions import db
from app.models.admin import Admin
from app.models.token import TokenBlocklist
from app.validation import json_body, required_str

bp = Blueprint("admin_auth", __name__, url_prefix="/api/admin/auth")

#: Compared against when no account matches, so a wrong email and a wrong
#: password take the same amount of time (§16 — no user enumeration).
_DUMMY_HASH = generate_password_hash("not-a-real-password")


class AccountLockedError(ApiError):
    status_code = 429
    code = "ACCOUNT_LOCKED"
    message = "Too many failed attempts. Try again shortly."


def _admin_claims(admin: Admin) -> dict[str, str]:
    return {"role": str(admin.role), "email": admin.email}


def _serialise(admin: Admin) -> dict[str, object]:
    return {
        "id": admin.id,
        "email": admin.email,
        "name": admin.name,
        "role": str(admin.role),
    }


@bp.post("/login")
def login():
    body = json_body()
    email = required_str(body, "email").lower()
    password = required_str(body, "password", max_length=256)

    admin = db.session.scalar(select(Admin).where(Admin.email == email))

    if admin is None:
        # Burn the same time as a real check before failing.
        from werkzeug.security import check_password_hash

        check_password_hash(_DUMMY_HASH, password)
        raise AuthenticationError()

    if admin.is_locked:
        raise AccountLockedError()

    if not admin.is_active:
        raise PermissionError_("This account has been deactivated.")

    if not admin.check_password(password):
        admin.register_failed_login(
            current_app.config["MAX_FAILED_LOGINS"],
            current_app.config["LOGIN_LOCKOUT_MINUTES"],
        )
        db.session.commit()
        raise AuthenticationError()

    admin.register_successful_login()
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
@jwt_required(refresh=True)
def refresh():
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
    token = get_jwt()
    db.session.add(
        TokenBlocklist(
            jti=token["jti"],
            token_type=token["type"],
            admin_id=int(token["sub"]),
            expires_at=datetime.fromtimestamp(token["exp"], tz=timezone.utc),
        )
    )
    db.session.commit()
    return jsonify({"success": True, "data": {"revoked": token["type"]}})


@bp.get("/me")
@jwt_required()
def me():
    admin = db.session.get(Admin, int(get_jwt_identity()))
    if admin is None or not admin.is_active:
        raise PermissionError_("This account is no longer active.")
    return jsonify({"success": True, "data": {"admin": _serialise(admin)}})
