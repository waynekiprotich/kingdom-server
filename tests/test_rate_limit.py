"""Rate limiting and response hardening (spec §16)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.extensions import db
from app.models.rate_limit import RateLimitCounter
from app.services import rate_limit
from tests.conftest import TEST_PASSWORD


@pytest.fixture
def tight(app):
    """Two requests per minute, so a test can reach the limit in three calls."""
    app.config["RATE_LIMITS"] = {
        "login": (2, 60),
        "orders": (2, 60),
        "refresh": (2, 60),
        "upload-signature": (2, 60),
    }
    return app


def _login(client, password=TEST_PASSWORD):
    return client.post(
        "/api/admin/auth/login",
        json={"email": "owner@example.com", "password": password},
    )


def test_repeated_sign_in_attempts_are_eventually_refused(client, admin, tight):
    assert _login(client, "wrong").status_code == 401
    assert _login(client, "wrong").status_code == 401

    refused = _login(client, "wrong")

    assert refused.status_code == 429
    assert refused.get_json()["error"]["code"] == "RATE_LIMITED"


def test_a_failed_attempt_still_counts(client, admin, tight):
    """The case worth guarding.

    A failed sign-in rolls its session back. If the counter were written on
    that session it would roll back too, and the attempts we most want to
    count would be the only ones that never were.
    """
    _login(client, "wrong")

    assert rate_limit.current_hits("login:127.0.0.1", 60) == 1


def test_signing_in_clears_the_counter(client, admin, tight):
    _login(client, "wrong")
    assert rate_limit.current_hits("login:127.0.0.1", 60) == 1

    assert _login(client).status_code == 200

    assert rate_limit.current_hits("login:127.0.0.1", 60) == 0


def test_placing_orders_is_limited(client, tight, variant):
    payload = {
        "items": [{"variant_id": variant.id, "quantity": 1}],
        "customer_name": "Achieng Odhiambo",
        "customer_phone": "0712345678",
        "delivery_location": "Kilimani, Nairobi",
    }

    assert client.post("/api/orders", json=payload).status_code == 201
    assert client.post("/api/orders", json=payload).status_code == 201

    assert client.post("/api/orders", json=payload).status_code == 429


def test_a_rejected_order_counts_too(client, tight, variant):
    """Otherwise the cheapest way to spam is to send requests that fail."""
    bad = {"items": [], "customer_name": "A", "customer_phone": "0712345678",
           "delivery_location": "Nairobi"}

    client.post("/api/orders", json=bad)
    client.post("/api/orders", json=bad)

    assert client.post("/api/orders", json=bad).status_code == 429


def test_separate_buckets_do_not_share_an_allowance(client, admin, tight):
    _login(client, "wrong")
    _login(client, "wrong")
    assert _login(client, "wrong").status_code == 429

    # Orders are counted separately, so a spent login allowance does not
    # close the shop.
    assert client.post("/api/orders", json={}).status_code != 429


def test_the_limiter_fails_open_when_its_table_is_missing(client, admin, tight):
    """A forgotten migration must not take the API down.

    Losing rate limiting is bad; refusing every request because the limiter
    cannot count is worse.
    """
    db.session.execute(text("DROP TABLE rate_limit_counters"))
    db.session.commit()

    for _ in range(5):
        assert _login(client, "wrong").status_code == 401


def test_one_row_per_bucket_and_window(client, admin, tight):
    """The table tracks live callers, not a log of every request ever made."""
    _login(client, "wrong")
    _login(client, "wrong")

    counters = db.session.scalars(select(RateLimitCounter)).all()

    assert len(counters) == 1
    assert counters[0].hits == 2


# --- response headers -------------------------------------------------------


def test_responses_carry_hardening_headers(client):
    response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def test_personal_data_is_never_cached(client, auth_headers):
    assert client.get("/api/admin/orders", headers=auth_headers).headers[
        "Cache-Control"
    ] == "no-store"


def test_the_public_catalog_stays_cacheable(client):
    """no-store on the busiest, least sensitive endpoint would be a
    performance cost with nothing bought for it."""
    cache_control = client.get("/api/products").headers["Cache-Control"]

    assert "no-store" not in cache_control
    assert "public" in cache_control


def test_hsts_is_only_promised_in_production(client):
    """Sending it from a local http:// server would be a lie the browser
    remembers for a year."""
    assert "Strict-Transport-Security" not in client.get("/health").headers
