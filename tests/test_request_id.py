"""Request correlation (spec §14, §26).

The property under test is operational rather than functional: nothing a
customer sees changes, but a failure they report becomes findable in the log
instead of being one anonymous traceback among an evening's worth.
"""

from __future__ import annotations

import pytest


def test_every_response_carries_a_request_id(client):
    assert client.get("/health").headers["X-Request-Id"]


def test_each_request_gets_its_own_id(client):
    first = client.get("/health").headers["X-Request-Id"]
    second = client.get("/health").headers["X-Request-Id"]

    assert first != second


def test_an_id_from_the_proxy_is_kept(client):
    """Render stamps inbound requests. Reusing its value means our logs and
    the platform's name the same request."""
    response = client.get("/health", headers={"X-Request-Id": "render-abc123"})

    assert response.headers["X-Request-Id"] == "render-abc123"


def test_an_absurd_inbound_id_is_truncated(client):
    """It reaches the log format, so it does not get to be unbounded."""
    response = client.get("/health", headers={"X-Request-Id": "x" * 500})

    assert len(response.headers["X-Request-Id"]) <= 64


# --- the reference on a failure ---------------------------------------------


@pytest.fixture
def exploding(app):
    """A route that fails the way an unforeseen bug would."""

    @app.get("/boom")
    def boom():
        raise RuntimeError("a bug nobody wrote a handler for")

    app.config["PROPAGATE_EXCEPTIONS"] = False
    return app


def test_a_server_error_returns_a_reference_the_customer_can_quote(client, exploding):
    response = client.get("/boom")

    assert response.status_code == 500
    assert response.get_json()["error"]["reference"]


def test_the_reference_matches_the_header(client, exploding):
    """Otherwise the number on the customer's screen finds nothing."""
    response = client.get("/boom")

    assert response.get_json()["error"]["reference"] == response.headers["X-Request-Id"]


def test_a_server_error_still_reveals_nothing_about_the_cause(client, exploding):
    """§16: the reference identifies the failure without describing it."""
    body = response_text = client.get("/boom").get_data(as_text=True)

    assert "RuntimeError" not in body
    assert "nobody wrote a handler" not in response_text


def test_the_error_envelope_keeps_its_shape(client, exploding):
    """`reference` is an addition, not a replacement — clients branch on
    `code` and must keep working (§14)."""
    error = client.get("/boom").get_json()["error"]

    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"]
