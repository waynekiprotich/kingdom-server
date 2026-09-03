"""A reference that survives from a customer's screen to the log line.

The gap this closes: a customer says "it failed when I tried to pay, about an
hour ago". The server logged a stack trace for that failure, but nothing ties
the two together — and on a busy evening there is no way to tell which of the
hour's tracebacks was theirs.

Every request now gets a short id. It goes into the log record, into the
``X-Request-Id`` response header, and — on an error — into the response body
as ``reference``, which the storefront can show. Finding the failure becomes a
search for one string.

Kept deliberately small: no dependency, no context propagation beyond this
process, and nothing that has to be running for the API to serve traffic.
"""

from __future__ import annotations

import logging
import uuid

from flask import Flask, g, has_request_context, request

#: Render (and most proxies) already stamp inbound requests. Reusing theirs
#: means our logs and the platform's line up on the same value.
INBOUND_HEADERS = ("X-Request-Id", "X-Correlation-Id")

#: Short enough to read over the phone, long enough not to collide within the
#: retention window of a log viewer.
ID_LENGTH = 8


def current() -> str | None:
    """The id for the request in flight, if there is one."""
    if not has_request_context():
        return None
    return g.get("request_id")


def _inbound() -> str | None:
    for header in INBOUND_HEADERS:
        value = request.headers.get(header)
        if value:
            # Never log an unbounded attacker-supplied string.
            return value[:64]
    return None


class RequestIdFilter(logging.Filter):
    """Puts the id on every record so the formatter can print it.

    A filter rather than an adapter: this way it applies to log lines from
    anywhere — our modules, SQLAlchemy, Werkzeug — without each caller having
    to remember to pass it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current() or "-"
        return True


def register_request_id(app: Flask) -> None:
    @app.before_request
    def assign_request_id():
        g.request_id = _inbound() or uuid.uuid4().hex[:ID_LENGTH]

    @app.after_request
    def expose_request_id(response):
        request_id = current()
        if request_id:
            # Visible in the browser's network panel, so a bug report can
            # quote it without the customer needing to find anything.
            response.headers.setdefault("X-Request-Id", request_id)
        return response
