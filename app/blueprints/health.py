"""Liveness and readiness (spec §26)."""

from __future__ import annotations

from flask import Blueprint, jsonify
from sqlalchemy import text

from app.extensions import db

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """Is the process up? Deliberately does not touch the database."""
    return jsonify({"success": True, "data": {"status": "ok"}})


@bp.get("/health/ready")
def ready():
    """Is the process able to serve traffic? Checks the database connection."""
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        return (
            jsonify(
                {
                    "success": False,
                    "error": {
                        "code": "SERVICE_UNAVAILABLE",
                        "message": "The database is not reachable.",
                    },
                }
            ),
            503,
        )
    return jsonify({"success": True, "data": {"status": "ready"}})
