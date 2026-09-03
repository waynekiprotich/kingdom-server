"""The single error contract for the API (spec §14).

Every failure leaves this application as::

    {"success": false, "error": {"code": "...", "message": "..."}}

Codes are machine-readable and stable; messages are for humans and may change.
Clients branch on ``code``, never on message text.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, jsonify, request
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from app.extensions import db
from app.request_id import current as current_request_id

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """An error we chose to raise, with a code the frontend can act on."""

    status_code = 400
    code = "BAD_REQUEST"
    message = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code

    def to_response(self) -> tuple[Any, int]:
        payload = {
            "success": False,
            "error": {"code": self.code, "message": self.message},
        }
        return jsonify(payload), self.status_code


class ValidationError(ApiError):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "Some of the information provided is not valid."


class AuthenticationError(ApiError):
    status_code = 401
    code = "AUTHENTICATION_FAILED"
    message = "Email or password is incorrect."


class PermissionError_(ApiError):
    status_code = 403
    code = "FORBIDDEN"
    message = "You do not have permission to do that."


class NotFoundError(ApiError):
    status_code = 404
    code = "NOT_FOUND"
    message = "The requested resource does not exist."


class ConflictError(ApiError):
    status_code = 409
    code = "CONFLICT"
    message = "That action conflicts with the current state of the resource."


class RateLimitError(ApiError):
    status_code = 429
    code = "RATE_LIMITED"
    message = "Too many attempts. Please wait and try again."


# Fallback codes for framework-raised HTTP errors, so a 404 from the router
# looks the same to a client as a 404 we raised ourselves.
_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _envelope(code: str, message: str, status: int, *, reference: str | None = None):
    error: dict[str, Any] = {"code": code, "message": message}
    if reference:
        # An extra field, never a replacement: clients still branch on `code`
        # (§14). This is only so a customer can quote something findable.
        error["reference"] = reference
    return jsonify({"success": False, "error": error}), status


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return error.to_response()

    @app.errorhandler(IntegrityError)
    def handle_integrity_error(error: IntegrityError):
        """A database constraint said no.

        Endpoints check the common cases up front and return a specific
        message. This is the backstop for the rest — including races two
        requests can lose — so a constraint violation is a 409, never a 500.
        The session is rolled back here because a failed transaction cannot be
        reused for the response.
        """
        db.session.rollback()
        logger.warning("Integrity error: %s", error.orig)
        return _envelope(
            "CONFLICT",
            "That change conflicts with existing data. It may already exist.",
            409,
        )

    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException):
        status = error.code or 500
        code = _HTTP_CODES.get(status, "HTTP_ERROR")
        return _envelope(code, error.description or "Request failed.", status)

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception):
        # Log the detail, return none of it — internals never reach a client
        # (§16). What does go back is the request id, which is not internal
        # detail: it identifies the failure without describing it, and turns
        # "it broke earlier" into a single log search.
        #
        # A failed request may have left the session unusable; roll it back so
        # the next request on this connection is not poisoned by it.
        db.session.rollback()
        logger.exception(
            "Unhandled exception on %s %s: %s", request.method, request.path, error
        )
        return _envelope(
            "INTERNAL_ERROR",
            "Something went wrong on our side. Please try again.",
            500,
            reference=current_request_id(),
        )
