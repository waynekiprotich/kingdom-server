"""Customer accounts: sign up, sign in, and see your own orders.

Optional throughout. Every endpoint here adds convenience to a shop that works
perfectly well without any of it — guest checkout is untouched, and nothing in
this module is required to place or pay for an order.

Two rules run through it:

* **A customer is not an admin.** Tokens carry an ``actor`` claim and the
  guards demand it; see ``app/authz.py`` for why that is the load-bearing part.
* **An account does not grant access to orders it did not place.** Orders link
  to an account at checkout, or are claimed with the confirmation token that
  already proves ownership. There is no lookup by phone or by name.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, g, jsonify
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from werkzeug.security import check_password_hash, generate_password_hash

from app.authz import (
    ACTOR_CUSTOMER,
    actor_claims,
    current_customer,
    customer_required,
    require_actor,
)
from app.errors import ApiError, AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.extensions import db
from app.models.customer import Customer
from app.models.order import Order
from app.models.token import TokenBlocklist
from app.serializers import serialize_customer, serialize_order_confirmation
from app.services import rate_limit
from app.utils import normalise_kenyan_phone
from app.validation import MISSING, body_str, json_body, reject_unknown_fields

bp = Blueprint("account", __name__, url_prefix="/api/account")

#: Compared against when no account matches, so a wrong email and a wrong
#: password cost the same time and neither confirms an address exists (§16).
_DUMMY_HASH = generate_password_hash("not-a-real-password")

#: Long enough to be worth having, short enough not to push people towards
#: reusing something they have already leaked elsewhere. Length is the only
#: rule: composition requirements demonstrably produce worse passwords.
MIN_PASSWORD_LENGTH = 10


class AccountLockedError(ApiError):
    status_code = 429
    code = "ACCOUNT_LOCKED"
    message = "Too many failed attempts. Try again shortly."


def _tokens(customer: Customer) -> dict[str, str]:
    identity = str(customer.id)
    claims = actor_claims(ACTOR_CUSTOMER)
    return {
        "access_token": create_access_token(identity, additional_claims=claims),
        "refresh_token": create_refresh_token(identity, additional_claims=claims),
    }


def _password(body: dict) -> str:
    password = body_str(body, "password", required=True, max_length=256)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return password


def _optional_phone(body: dict) -> str | None:
    """A normalised phone number, or ``None`` when absent, null or blank.

    Stored as ``2547XXXXXXXX`` like every other phone number in the system, so
    prefilling checkout hands the M-Pesa push something it can already use.
    """
    raw = body_str(body, "phone", max_length=20, nullable=True)
    if raw is MISSING or raw is None or not raw.strip():
        return None

    phone = normalise_kenyan_phone(raw)
    if phone is None:
        raise ValidationError(
            "phone must be a valid Kenyan mobile number, e.g. 0712345678."
        )
    return phone


# --- signing up and in ------------------------------------------------------


@bp.post("/register")
@rate_limit.limit("register")
def register():
    body = json_body()
    reject_unknown_fields(body, ("email", "password", "name", "phone"))

    email = body_str(body, "email", required=True, max_length=255).lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValidationError("email must be a valid email address.")

    name = body_str(body, "name", required=True, max_length=120)
    password = _password(body)
    phone = _optional_phone(body)

    existing = db.session.scalar(select(Customer.id).where(Customer.email == email))
    if existing is not None:
        # This does disclose that an address is registered, which is the
        # unavoidable cost of telling someone their sign-up failed. The sign-in
        # path above is where enumeration actually matters, and that one is
        # constant-time and says nothing.
        raise ConflictError(
            "An account with that email already exists. Try signing in.",
            code="EMAIL_TAKEN",
        )

    customer = Customer(
        email=email,
        name=name,
        phone=phone,
    )
    customer.set_password(password)
    db.session.add(customer)
    db.session.commit()

    return (
        jsonify(
            {
                "success": True,
                "data": {"customer": serialize_customer(customer), **_tokens(customer)},
            }
        ),
        201,
    )


@bp.post("/login")
@rate_limit.limit("login")
def login():
    body = json_body()
    reject_unknown_fields(body, ("email", "password"))

    email = body_str(body, "email", required=True, max_length=255).lower()
    password = body_str(body, "password", required=True, max_length=256)

    customer = db.session.scalar(select(Customer).where(Customer.email == email))

    if customer is None:
        # Burn the same time as a real check, so a missing account and a wrong
        # password are indistinguishable from the outside.
        check_password_hash(_DUMMY_HASH, password)
        raise AuthenticationError()

    if customer.is_locked:
        raise AccountLockedError()

    if not customer.is_active:
        raise AuthenticationError()

    if not customer.check_password(password):
        customer.register_failed_login(
            current_app.config["MAX_FAILED_LOGINS"],
            current_app.config["LOGIN_LOCKOUT_MINUTES"],
        )
        db.session.commit()
        raise AuthenticationError()

    customer.register_successful_login()
    db.session.commit()
    rate_limit.reset(f"login:{rate_limit.client_ip()}")

    return jsonify(
        {
            "success": True,
            "data": {"customer": serialize_customer(customer), **_tokens(customer)},
        }
    )


@bp.post("/refresh")
@rate_limit.limit("refresh")
@jwt_required(refresh=True)
def refresh():
    require_actor(ACTOR_CUSTOMER)

    customer = db.session.get(Customer, int(get_jwt_identity()))
    if customer is None or not customer.is_active:
        raise AuthenticationError("This account is no longer active.")

    return jsonify(
        {
            "success": True,
            "data": {
                "access_token": create_access_token(
                    str(customer.id), additional_claims=actor_claims(ACTOR_CUSTOMER)
                )
            },
        }
    )


@bp.post("/logout")
@jwt_required(verify_type=False)
def logout():
    """Revoke the presented token. Called once per token; the client discards
    both regardless."""
    require_actor(ACTOR_CUSTOMER)

    token = get_jwt()
    db.session.add(
        TokenBlocklist(
            jti=token["jti"],
            token_type=token["type"],
            customer_id=int(token["sub"]),
            expires_at=datetime.fromtimestamp(token["exp"], tz=timezone.utc),
        )
    )
    db.session.commit()
    return jsonify({"success": True, "data": {"revoked": token["type"]}})


# --- the account itself -----------------------------------------------------


@bp.get("/me")
@customer_required()
def me():
    return jsonify({"success": True, "data": {"customer": serialize_customer(g.customer)}})


@bp.patch("/me")
@customer_required()
def update_me():
    body = json_body()
    reject_unknown_fields(body, ("name", "phone"))

    name = body_str(body, "name", max_length=120)
    if name is not MISSING:
        if not name:
            raise ValidationError("name is required.")
        g.customer.name = name

    if "phone" in body:
        g.customer.phone = _optional_phone(body)

    db.session.commit()
    return jsonify({"success": True, "data": {"customer": serialize_customer(g.customer)}})


# --- order history ----------------------------------------------------------


@bp.get("/orders")
@customer_required()
def my_orders():
    """Every order this account owns, newest first.

    Scoped by ``customer_id`` and nothing else. There is no filter, parameter
    or header that widens it — the only way an order appears here is by having
    been placed while signed in, or claimed with its confirmation token.
    """
    orders = db.session.scalars(
        select(Order)
        .where(Order.customer_id == g.customer.id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .options(selectinload(Order.items))
    ).all()

    return jsonify(
        {
            "success": True,
            "data": {
                "orders": [serialize_order_confirmation(order) for order in orders]
            },
        }
    )


@bp.post("/orders/claim")
@rate_limit.limit("claim")
def claim_order():
    """Attach an order placed as a guest to this account.

    The confirmation token is the credential, exactly as it is for the guest
    confirmation page: holding it is what proves you are the person who placed
    the order. Nothing is matched on phone number or name — "an account with
    your phone number can see your orders" is a data breach with a friendly
    name, and it is the reason this endpoint exists instead.
    """
    customer = current_customer()
    if customer is None:
        raise AuthenticationError("Sign in to claim an order.")

    body = json_body()
    reject_unknown_fields(body, ("confirmation_token",))
    token = body_str(body, "confirmation_token", required=True, max_length=64)

    order = db.session.scalar(
        select(Order)
        .where(Order.confirmation_token == token)
        .options(selectinload(Order.items))
    )
    if order is None:
        raise NotFoundError(
            "No order matches that confirmation link.", code="ORDER_NOT_FOUND"
        )

    if order.customer_id is not None and order.customer_id != customer.id:
        # Someone else got there first. Refusing rather than reassigning: the
        # token may have been shared, and the earlier claim is the one with a
        # history behind it.
        raise ConflictError(
            "That order is already linked to another account.",
            code="ORDER_ALREADY_CLAIMED",
        )

    order.customer_id = customer.id
    db.session.commit()

    return jsonify(
        {"success": True, "data": {"order": serialize_order_confirmation(order)}}
    )
