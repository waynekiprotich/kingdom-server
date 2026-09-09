"""Customer accounts, and the wall between them and the admin dashboard.

The first section is the one that matters. Admins and customers authenticate
against the same signing key with a numeric identity, so without a claim saying
which kind of account a token belongs to, customer #1 and admin #1 hold
interchangeable credentials. Every test under "the wall" is a specific way that
could have gone wrong.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.extensions import db
from app.models.customer import Customer
from app.models.order import Order, OrderStatus

PASSWORD = "correct-horse-battery"


def _register(client, **overrides):
    body = {
        "email": "new@example.com",
        "password": PASSWORD,
        "name": "New Shopper",
        **overrides,
    }
    return client.post("/api/account/register", json=body)


# --- the wall between customers and admins ---------------------------------


def test_a_customer_token_cannot_reach_an_admin_endpoint(client, customer_headers, admin):
    """The whole reason app/authz.py has an actor claim.

    The customer and the admin fixtures both have id 1 in a fresh database, so
    without the claim this token would load the admin and pass every check.
    """
    for path in (
        "/api/admin/orders",
        "/api/admin/products",
        "/api/admin/categories",
        "/api/admin/customers",
    ):
        response = client.get(path, headers=customer_headers)
        assert response.status_code == 403, f"{path} accepted a customer token"


def test_a_customer_refresh_token_cannot_mint_an_admin_access_token(
    client, customer_tokens, admin
):
    """The sharpest edge: /api/admin/auth/refresh loads an Admin by the token's
    identity, so an unchecked customer refresh token would hand back a working
    admin session."""
    response = client.post(
        "/api/admin/auth/refresh",
        headers={"Authorization": f"Bearer {customer_tokens['refresh_token']}"},
    )

    assert response.status_code == 403
    assert "access_token" not in (response.get_json().get("data") or {})


def test_a_customer_token_cannot_read_the_admin_profile(client, customer_headers, admin):
    response = client.get("/api/admin/auth/me", headers=customer_headers)

    assert response.status_code == 403


def test_an_admin_token_cannot_reach_customer_endpoints(client, auth_headers):
    """The mirror. An admin is not silently also every customer."""
    for path in ("/api/account/me", "/api/account/orders"):
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 403, f"{path} accepted an admin token"


def test_an_admin_token_cannot_be_used_to_claim_an_order(client, auth_headers, order):
    response = client.post(
        "/api/account/orders/claim",
        headers=auth_headers,
        json={"confirmation_token": order.confirmation_token},
    )

    assert response.status_code in (401, 403)
    db.session.expire_all()
    assert db.session.get(Order, order.id).customer_id is None


# --- signing up -------------------------------------------------------------


def test_registering_returns_tokens_and_the_account(client):
    response = _register(client)

    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["customer"]["email"] == "new@example.com"
    assert data["access_token"] and data["refresh_token"]


def test_a_password_hash_never_leaves_the_server(client, customer_headers):
    body = client.get("/api/account/me", headers=customer_headers).get_json()

    assert "password_hash" in Customer.__table__.columns  # it exists...
    assert "password_hash" not in body["data"]["customer"]  # ...and never ships
    assert "failed_login_count" not in body["data"]["customer"]


def test_a_duplicate_email_is_refused(client, customer):
    response = _register(client, email=customer.email)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "EMAIL_TAKEN"


def test_email_is_stored_lowercase_so_case_cannot_duplicate_an_account(client):
    _register(client, email="Mixed.Case@Example.COM")
    again = _register(client, email="mixed.case@example.com")

    assert again.status_code == 409


@pytest.mark.parametrize("password", ["short", "123456789"])
def test_a_short_password_is_refused(client, password):
    assert _register(client, password=password).status_code == 422


def test_a_phone_number_is_normalised_on_the_account(client):
    _register(client, phone="0712 345 678")

    account = db.session.scalar(
        db.select(Customer).where(Customer.email == "new@example.com")
    )
    assert account.phone == "254712345678"


def test_a_malformed_phone_is_refused(client):
    assert _register(client, phone="12345").status_code == 422


# --- signing in -------------------------------------------------------------


def test_signing_in_with_the_wrong_password_fails(client, customer):
    response = client.post(
        "/api/account/login", json={"email": customer.email, "password": "wrong-one"}
    )

    assert response.status_code == 401


def test_an_unknown_email_and_a_wrong_password_are_indistinguishable(client, customer):
    """No user enumeration: both answers must look the same to a caller."""
    unknown = client.post(
        "/api/account/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    wrong = client.post(
        "/api/account/login", json={"email": customer.email, "password": "wrong-one"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.get_json()["error"] == wrong.get_json()["error"]


def test_repeated_failures_lock_the_account(client, customer, app):
    for _ in range(app.config["MAX_FAILED_LOGINS"]):
        client.post(
            "/api/account/login",
            json={"email": customer.email, "password": "wrong-one"},
        )

    locked = client.post(
        "/api/account/login", json={"email": customer.email, "password": PASSWORD}
    )

    assert locked.status_code == 429
    assert locked.get_json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_a_deactivated_account_cannot_sign_in(client, customer):
    customer.is_active = False
    db.session.commit()

    response = client.post(
        "/api/account/login", json={"email": customer.email, "password": PASSWORD}
    )

    assert response.status_code == 401


def test_a_deactivated_account_loses_access_before_its_token_expires(
    client, customer, customer_headers
):
    """Deactivating must not wait for the token to lapse."""
    assert client.get("/api/account/me", headers=customer_headers).status_code == 200

    customer.is_active = False
    db.session.commit()

    assert client.get("/api/account/me", headers=customer_headers).status_code == 403


def test_a_revoked_token_stops_working(client, customer_tokens):
    headers = {"Authorization": f"Bearer {customer_tokens['access_token']}"}
    assert client.post("/api/account/logout", headers=headers).status_code == 200

    assert client.get("/api/account/me", headers=headers).status_code == 401


# --- orders and accounts ----------------------------------------------------


def _checkout(client, variant, headers=None):
    return client.post(
        "/api/orders",
        headers=headers or {},
        json={
            "items": [{"variant_id": variant.id, "quantity": 1}],
            "customer_name": "Amina Wanjiru",
            "customer_phone": "0712345678",
            "delivery_location": "Kilimani, Nairobi",
        },
    )


def test_checking_out_signed_in_links_the_order_to_the_account(
    client, variant, customer, customer_headers
):
    response = _checkout(client, variant, customer_headers)

    assert response.status_code == 201
    order = db.session.scalar(db.select(Order))
    assert order.customer_id == customer.id


def test_guest_checkout_still_works_and_owns_no_account(client, variant):
    """The rule this whole feature had to not break."""
    response = _checkout(client, variant)

    assert response.status_code == 201
    assert db.session.scalar(db.select(Order)).customer_id is None


def test_a_stale_token_does_not_block_a_sale(client, variant):
    """A junk Authorization header must read as 'guest', never as an error —
    an expired token sitting in someone's browser cannot be allowed to stop
    them buying."""
    response = _checkout(client, variant, {"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 201
    assert db.session.scalar(db.select(Order)).customer_id is None


def test_my_orders_shows_only_my_own(client, variant, customer, customer_headers):
    _checkout(client, variant, customer_headers)  # mine
    _checkout(client, variant)  # someone else's, as a guest

    body = client.get("/api/account/orders", headers=customer_headers).get_json()

    assert len(body["data"]["orders"]) == 1


def test_order_history_is_empty_for_a_new_account(client, customer_headers):
    body = client.get("/api/account/orders", headers=customer_headers).get_json()

    assert body["data"]["orders"] == []


# --- claiming a guest order -------------------------------------------------


def test_a_guest_order_can_be_claimed_with_its_confirmation_token(
    client, variant, customer, customer_headers
):
    placed = _checkout(client, variant).get_json()["data"]

    response = client.post(
        "/api/account/orders/claim",
        headers=customer_headers,
        json={"confirmation_token": placed["confirmation_token"]},
    )

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.scalar(db.select(Order)).customer_id == customer.id


def test_claiming_needs_the_token_not_the_order_number(
    client, variant, customer_headers
):
    """The order number is sequential and shown in the dashboard. If it worked
    here, anyone could walk KC-100001 upwards and absorb the shop's orders."""
    placed = _checkout(client, variant).get_json()["data"]

    response = client.post(
        "/api/account/orders/claim",
        headers=customer_headers,
        json={"confirmation_token": placed["order"]["order_number"]},
    )

    assert response.status_code == 404


def test_an_order_already_claimed_by_someone_else_is_refused(
    client, variant, customer_headers
):
    other = Customer(email="other@example.com", name="Other")
    other.set_password(PASSWORD)
    db.session.add(other)
    db.session.commit()

    placed = _checkout(client, variant).get_json()["data"]
    order = db.session.scalar(db.select(Order))
    order.customer_id = other.id
    db.session.commit()

    response = client.post(
        "/api/account/orders/claim",
        headers=customer_headers,
        json={"confirmation_token": placed["confirmation_token"]},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "ORDER_ALREADY_CLAIMED"


def test_claiming_requires_signing_in(client, variant):
    placed = _checkout(client, variant).get_json()["data"]

    response = client.post(
        "/api/account/orders/claim",
        json={"confirmation_token": placed["confirmation_token"]},
    )

    assert response.status_code == 401


def test_matching_phone_numbers_do_not_grant_access_to_an_order(
    client, variant, customer, customer_headers
):
    """The design decision this feature turns on.

    The guest order below carries the same phone number as the account. It must
    still not appear in that account's history — otherwise signing up with
    someone else's number would hand you their name, address and order
    history.
    """
    _checkout(client, variant)  # guest order, phone 254712345678
    assert customer.phone == "254712345678"

    body = client.get("/api/account/orders", headers=customer_headers).get_json()

    assert body["data"]["orders"] == []


# --- the admin's view of customers -----------------------------------------


def test_an_admin_can_list_customers(client, auth_headers, customer):
    body = client.get("/api/admin/customers", headers=auth_headers).get_json()

    listed = body["data"]["customers"]
    assert len(listed) == 1
    assert listed[0]["email"] == customer.email
    assert "password_hash" not in listed[0]


def test_customer_totals_count_only_settled_orders(
    client, auth_headers, variant, customer, customer_headers
):
    _checkout(client, variant, customer_headers)
    order = db.session.scalar(db.select(Order))
    order.status = OrderStatus.DELIVERED
    db.session.commit()

    body = client.get(
        f"/api/admin/customers/{customer.id}", headers=auth_headers
    ).get_json()

    assert body["data"]["customer"]["order_count"] == 1
    assert Decimal(body["data"]["customer"]["total_spent"]) == order.total


def test_an_unpaid_order_is_not_counted_as_spend(
    client, auth_headers, variant, customer, customer_headers
):
    _checkout(client, variant, customer_headers)  # lands in PAYMENT_PENDING

    body = client.get(
        f"/api/admin/customers/{customer.id}", headers=auth_headers
    ).get_json()

    assert body["data"]["customer"]["order_count"] == 1
    assert Decimal(body["data"]["customer"]["total_spent"]) == 0


def test_an_admin_can_deactivate_a_customer(client, auth_headers, customer):
    response = client.patch(
        f"/api/admin/customers/{customer.id}",
        headers=auth_headers,
        json={"is_active": False},
    )

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(Customer, customer.id).is_active is False


def test_customer_endpoints_are_never_publicly_cached(client, customer_headers):
    response = client.get("/api/account/me", headers=customer_headers)

    assert response.headers["Cache-Control"] == "no-store"
