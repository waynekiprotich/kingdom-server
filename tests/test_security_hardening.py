"""Security hardening: the controls added after the production audit.

Each test names the failure it prevents. Several are attacks that worked
before the change they cover.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import jwt as pyjwt
import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import select

from app.config import ConfigError, get_config
from app.extensions import db
from app.models.audit import AuditLog
from app.models.catalog import Product, ProductVariant
from app.models.rate_limit import RateLimitCounter
from app.models.token import TokenBlocklist
from app.models.base import utcnow
from app.services import cloudinary, rate_limit
from tests.conftest import TEST_PASSWORD

STRONG_A = "a" * 48
STRONG_B = "b" * 48


@pytest.fixture
def product(variant):
    return db.session.get(Product, variant.product_id)


def _production_env(monkeypatch, **overrides):
    values = {
        "SECRET_KEY": STRONG_A,
        "JWT_SECRET_KEY": STRONG_B,
        "DATABASE_URL": "postgresql://user:pw@localhost/kingdom",
        "CORS_ORIGINS": "https://shop.example.com",
        "MPESA_MODE": "daraja",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _codes(response):
    return response.status_code, (response.get_json() or {}).get("error", {}).get("code")


# --- configuration ------------------------------------------------------------


@pytest.mark.parametrize(
    "origins",
    [
        "*",
        "http://shop.example.com",
        "https://shop.example.com/",
        "https://.*\\.example\\.com",
        "https://shop.example.com, *",
    ],
)
def test_production_refuses_loose_cors_origins(monkeypatch, origins):
    _production_env(monkeypatch, CORS_ORIGINS=origins)

    with pytest.raises(ConfigError):
        get_config("production")


def test_production_refuses_one_value_for_both_signing_keys(monkeypatch):
    _production_env(monkeypatch, JWT_SECRET_KEY=STRONG_A)

    with pytest.raises(ConfigError) as excinfo:
        get_config("production")

    assert "different" in str(excinfo.value)


def test_development_config_refuses_to_run_on_render(monkeypatch):
    """A Render service missing FLASK_ENV would otherwise run with the M-Pesa
    simulator enabled and debug on."""
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/kingdom")

    with pytest.raises(ConfigError):
        get_config("development")


# --- request size and malformed input -----------------------------------------


def test_an_oversized_body_is_refused_before_it_is_read(client):
    response = client.post(
        "/api/orders",
        data=b"{" + b" " * (1024 * 1024 + 10) + b"}",
        content_type="application/json",
    )

    assert _codes(response) == (413, "PAYLOAD_TOO_LARGE")
    assert set(response.get_json()) == {"success", "error"}


def test_malformed_json_is_a_validation_error(client):
    response = client.post(
        "/api/orders", data=b'{"items": [', content_type="application/json"
    )

    assert _codes(response) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e40", "sNaN"])
def test_non_finite_or_huge_price_filters_are_rejected_not_a_500(client, value):
    response = client.get(f"/api/products?min_price={value}")

    assert _codes(response) == (422, "VALIDATION_ERROR")


def test_a_price_beyond_the_column_is_a_validation_error(client, auth_headers, product):
    response = client.patch(
        f"/api/admin/products/{product.id}",
        json={"base_price": "99999999999999"},
        headers=auth_headers,
    )

    assert _codes(response) == (422, "VALIDATION_ERROR")


def test_search_wildcards_are_literal(client, variant):
    """"%" used to match every product."""
    assert client.get("/api/products?q=%25").get_json()["data"]["pagination"]["total"] == 0
    assert client.get("/api/products?q=Test").get_json()["data"]["pagination"]["total"] == 1


@pytest.mark.parametrize("value", ["bad id with spaces", "<script>", "a;b=c", "ünïcode"])
def test_unsafe_inbound_request_ids_are_replaced(client, value):
    """Echoed into a header and every log line, so only plain ids survive.
    (Raw CR/LF cannot be tested here: HTTP and the test client both reject it.)"""
    response = client.get("/health", headers={"X-Request-Id": value})

    assert response.headers["X-Request-Id"] != value


# --- tokens -------------------------------------------------------------------


def test_editing_a_customer_tokens_actor_claim_breaks_its_signature(
    client, app, customer_tokens, admin
):
    token = customer_tokens["access_token"]
    claims = pyjwt.decode(token, options={"verify_signature": False})
    claims["actor"] = "admin"
    forged = pyjwt.encode(claims, "attacker-guessed-key-that-is-long-enough!", algorithm="HS256")

    response = client.get("/api/admin/orders", headers={"Authorization": f"Bearer {forged}"})

    assert _codes(response) == (401, "INVALID_TOKEN")


def test_an_unsigned_alg_none_token_is_refused(client, customer_tokens, admin):
    claims = pyjwt.decode(customer_tokens["access_token"], options={"verify_signature": False})
    claims["actor"] = "admin"
    unsigned = pyjwt.encode(claims, None, algorithm="none")

    response = client.get("/api/admin/orders", headers={"Authorization": f"Bearer {unsigned}"})

    assert response.status_code == 401


def test_an_expired_token_is_refused(client, app, admin):
    with app.test_request_context():
        token = create_access_token(
            str(admin.id),
            additional_claims={"actor": "admin"},
            expires_delta=timedelta(seconds=-5),
        )

    response = client.get("/api/admin/orders", headers={"Authorization": f"Bearer {token}"})

    assert _codes(response) == (401, "TOKEN_EXPIRED")


def test_a_validly_signed_token_without_an_actor_is_refused(client, app, admin):
    with app.test_request_context():
        token = create_access_token(str(admin.id))

    response = client.get("/api/admin/orders", headers={"Authorization": f"Bearer {token}"})

    assert _codes(response) == (403, "FORBIDDEN")


def test_a_garbage_token_gets_a_generic_answer(client):
    response = client.get("/api/admin/orders", headers={"Authorization": "Bearer not.a.jwt"})

    assert _codes(response) == (401, "INVALID_TOKEN")
    assert "Traceback" not in response.get_data(as_text=True)


def test_a_customer_cannot_adjust_stock(client, customer_headers, variant):
    response = client.patch(
        f"/api/admin/variants/{variant.id}",
        json={"stock_quantity": 999},
        headers=customer_headers,
    )

    assert response.status_code == 403
    db.session.refresh(variant)
    assert variant.stock_quantity == 5


def test_admin_customer_listing_never_carries_password_material(
    client, auth_headers, customer
):
    body = client.get("/api/admin/customers", headers=auth_headers).get_data(as_text=True)

    assert "password" not in body
    assert "scrypt" not in body


# --- sign-in --------------------------------------------------------------------


def _admin_login(client, email="owner@example.com", password=TEST_PASSWORD):
    return client.post("/api/admin/auth/login", json={"email": email, "password": password})


def _audit(action):
    return db.session.scalars(select(AuditLog).where(AuditLog.action == action)).all()


def test_a_deactivated_admin_is_not_revealed_to_a_wrong_password(client, admin):
    """"Deactivated" used to be answered before the password was checked."""
    admin.is_active = False
    db.session.commit()

    response = _admin_login(client, password="wrong-password")

    assert _codes(response) == (401, "AUTHENTICATION_FAILED")


def test_admin_sign_in_events_are_audited_without_secrets(client, admin):
    _admin_login(client, email="nobody@example.com", password="hunter2-typed-here")
    _admin_login(client, password="wrong-password")
    tokens = _admin_login(client).get_json()["data"]
    client.post(
        "/api/admin/auth/logout",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    failed = _audit("auth.login_failed")
    assert len(failed) == 2
    unknown, wrong = failed
    # An unknown account stores nothing the caller typed.
    assert unknown.admin_id is None and unknown.admin_email is None
    assert wrong.admin_id == admin.id
    assert len(_audit("auth.login")) == 1
    assert len(_audit("auth.logout")) == 1

    everything = " ".join(
        f"{row.admin_email} {row.previous_value} {row.new_value}"
        for row in db.session.scalars(select(AuditLog))
    )
    assert "hunter2" not in everything
    assert TEST_PASSWORD not in everything
    assert tokens["access_token"] not in everything


def test_a_throwaway_account_cannot_reset_the_login_counter(
    client, app, admin, customer
):
    """The bypass: guess at an admin, sign in to your own account, repeat."""
    app.config["RATE_LIMITS"] = {**app.config["RATE_LIMITS"], "login": (3, 60)}

    _admin_login(client, password="guess-1")
    _admin_login(client, password="guess-2")
    client.post(
        "/api/account/login", json={"email": customer.email, "password": TEST_PASSWORD}
    )

    assert _codes(_admin_login(client, password="guess-3")) == (429, "RATE_LIMITED")


def test_audit_rows_ignore_a_forged_forwarded_for_header(client, auth_headers):
    """The raw header was stored — attacker-controlled, and long enough to
    overflow the column and fail the admin's change."""
    response = client.post(
        "/api/admin/categories",
        json={"name": "Dresses"},
        headers={**auth_headers, "X-Forwarded-For": ", ".join(["203.0.113.66"] * 20)},
    )

    assert response.status_code == 201
    row = _audit("category.create")[0]
    assert row.ip_address == "127.0.0.1"


# --- Cloudinary -------------------------------------------------------------------


@pytest.fixture
def cloudinary_configured(app):
    app.config["CLOUDINARY_CLOUD_NAME"] = "kingdom"
    app.config["CLOUDINARY_API_KEY"] = "123456789"
    app.config["CLOUDINARY_API_SECRET"] = "top-secret"
    return app


def test_upload_signature_restricts_formats_and_size(client, cloudinary_configured, auth_headers):
    data = client.post("/api/admin/images/upload-signature", headers=auth_headers).get_json()[
        "data"
    ]

    formats = data["allowed_formats"].split(",")
    assert "svg" not in formats and "pdf" not in formats and "jpg" in formats
    assert data["transformation"].startswith("c_limit")

    # The signature covers exactly what the browser will send, so none of the
    # restrictions can be dropped client-side.
    signed = {k: v for k, v in data.items() if k not in ("cloud_name", "api_key", "upload_url", "signature", "max_bytes")}
    assert cloudinary.sign(signed, "top-secret") == data["signature"]
    assert cloudinary.sign({**signed, "allowed_formats": "svg"}, "top-secret") != data["signature"]


def test_upload_signature_refuses_an_arbitrary_folder(client, cloudinary_configured, auth_headers):
    response = client.post(
        "/api/admin/images/upload-signature?folder=../elsewhere", headers=auth_headers
    )

    assert _codes(response) == (422, "VALIDATION_ERROR")


def test_a_customer_cannot_mint_an_upload_signature(
    client, cloudinary_configured, customer_headers
):
    response = client.post("/api/admin/images/upload-signature", headers=customer_headers)

    assert response.status_code == 403


@pytest.mark.parametrize(
    "public_id",
    ["w_5000/products/shirt", "products/shirt,e_blur", "../products/shirt", "/products/x", "a b"],
)
def test_public_ids_that_would_inject_transformations_are_refused(
    client, auth_headers, product, public_id
):
    response = client.post(
        f"/api/admin/products/{product.id}/images",
        json={"public_id": public_id, "alt_text": "Front"},
        headers=auth_headers,
    )

    assert _codes(response) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://example.com/a.jpg", "data:image/png;base64,AA"])
def test_external_image_urls_must_be_https(client, auth_headers, product, url):
    response = client.post(
        f"/api/admin/products/{product.id}/images",
        json={"url": url, "alt_text": "Front"},
        headers=auth_headers,
    )

    assert _codes(response) == (422, "VALIDATION_ERROR")


def test_a_normal_cloudinary_public_id_is_accepted(client, auth_headers, product):
    response = client.post(
        f"/api/admin/products/{product.id}/images",
        json={"public_id": "products/linen-shirt_front-v2", "alt_text": "Front"},
        headers=auth_headers,
    )

    assert response.status_code == 201


# --- checkout ---------------------------------------------------------------------


def _order(variant_id, quantity=1, **extra):
    return {
        "items": [{"variant_id": variant_id, "quantity": quantity}],
        "customer_name": "Achieng Odhiambo",
        "customer_phone": "0712345678",
        "delivery_location": "Kilimani, Nairobi",
        **extra,
    }


@pytest.mark.parametrize(
    "extra",
    [{"total": "1.00"}, {"subtotal": "1.00"}, {"status": "PAID"}, {"delivery_fee": "0"}],
)
def test_client_supplied_totals_or_status_are_refused(client, variant, extra):
    response = client.post("/api/orders", json=_order(variant.id, **extra))

    assert _codes(response) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "item",
    [
        {"variant_id": 1, "quantity": -1},
        {"variant_id": 1, "quantity": 0},
        {"variant_id": 1, "quantity": 1.5},
        {"variant_id": 1, "quantity": "2"},
        {"variant_id": "1", "quantity": 1},
        {"variant_id": 1, "quantity": 1, "price": "1.00"},
        {"variant_id": 1, "quantity": 1, "stock_quantity": 999},
    ],
)
def test_malformed_or_manipulated_line_items_are_refused(client, variant, item):
    item = {**item, "variant_id": variant.id} if isinstance(item["variant_id"], int) else item
    body = _order(variant.id)
    body["items"] = [item]

    assert client.post("/api/orders", json=body).status_code == 422


def test_concurrent_checkouts_cannot_oversell(app, variant):
    """Two shoppers racing for the whole remaining stock: exactly one wins."""
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Row locking is only meaningful on PostgreSQL.")

    variant_id, stock = variant.id, variant.stock_quantity
    db.session.commit()  # release anything the fixture's session still holds

    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def buy():
        barrier.wait()
        response = app.test_client().post("/api/orders", json=_order(variant_id, quantity=stock))
        statuses.append(response.status_code)

    threads = [threading.Thread(target=buy) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(statuses) == [201, 409]
    db.session.expire_all()
    assert db.session.get(ProductVariant, variant_id).stock_quantity == 0


# --- operations -------------------------------------------------------------------


def test_prune_removes_only_expired_security_rows(app, admin):
    db.session.add_all(
        [
            TokenBlocklist(
                jti="expired", token_type="access", admin_id=admin.id,
                expires_at=utcnow() - timedelta(hours=1),
            ),
            TokenBlocklist(
                jti="live", token_type="refresh", admin_id=admin.id,
                expires_at=utcnow() + timedelta(days=1),
            ),
            RateLimitCounter(
                bucket="login:1.2.3.4", window_start=utcnow() - timedelta(days=2), hits=3
            ),
        ]
    )
    db.session.commit()

    result = app.test_cli_runner().invoke(args=["prune-security-tables"])

    assert result.exit_code == 0, result.output
    assert db.session.scalars(select(TokenBlocklist.jti)).all() == ["live"]
    assert db.session.scalars(select(RateLimitCounter)).all() == []
