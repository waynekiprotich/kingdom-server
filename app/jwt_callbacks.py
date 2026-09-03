"""Make JWT failures speak the same error contract as everything else (§14).

Without these, flask-jwt-extended returns its own JSON shape and a client would
need two different ways to read an error.
"""

from __future__ import annotations

from flask import jsonify
from flask_jwt_extended import JWTManager
from sqlalchemy import select

from app.extensions import db
from app.models.token import TokenBlocklist


def _envelope(code: str, message: str, status: int):
    return jsonify({"success": False, "error": {"code": code, "message": message}}), status


def register_jwt_callbacks(jwt: JWTManager) -> None:
    @jwt.token_in_blocklist_loader
    def is_revoked(_header: dict, payload: dict) -> bool:
        jti = payload["jti"]
        return (
            db.session.scalar(select(TokenBlocklist.id).where(TokenBlocklist.jti == jti))
            is not None
        )

    @jwt.unauthorized_loader
    def missing_token(_reason: str):
        return _envelope(
            "AUTHENTICATION_REQUIRED", "Sign in to continue.", 401
        )

    @jwt.invalid_token_loader
    def invalid_token(_reason: str):
        return _envelope("INVALID_TOKEN", "Your session is not valid.", 401)

    @jwt.expired_token_loader
    def expired_token(_header: dict, _payload: dict):
        return _envelope("TOKEN_EXPIRED", "Your session has expired. Sign in again.", 401)

    @jwt.revoked_token_loader
    def revoked_token(_header: dict, _payload: dict):
        return _envelope("TOKEN_REVOKED", "Your session has ended.", 401)

    @jwt.needs_fresh_token_loader
    def needs_fresh(_header: dict, _payload: dict):
        return _envelope("FRESH_TOKEN_REQUIRED", "Please sign in again to continue.", 401)
