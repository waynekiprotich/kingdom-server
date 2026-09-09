"""Route guards for the two kinds of account that can sign in (spec §15).

**The important thing in this module is that a token says what it is for.**

Both admins and customers authenticate with a JWT whose identity is a numeric
primary key. Without a distinguishing claim, a customer's token for id 7 and an
admin's token for id 7 are byte-for-byte interchangeable in everything but the
signature — and the signature is the same key. A customer would simply be an
admin. So every token carries an ``actor`` claim, every guard demands the one
it expects, and a token with no ``actor`` at all is refused rather than assumed
to be either.

That last part matters on deploy: tokens minted before this existed have no
``actor`` and are rejected, so everyone signs in again once. Failing closed is
the only safe direction here.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import wraps

from flask import g
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required, verify_jwt_in_request

from app.errors import PermissionError_
from app.extensions import db
from app.models.admin import Admin, AdminRole
from app.models.customer import Customer

#: The claim name, and the two values it may take. Anything else — including
#: its absence — is not a valid actor.
ACTOR_CLAIM = "actor"
ACTOR_ADMIN = "admin"
ACTOR_CUSTOMER = "customer"


def actor_claims(actor: str) -> dict[str, str]:
    """The claims every token must carry. Use this when minting one."""
    return {ACTOR_CLAIM: actor}


def require_actor(expected: str) -> None:
    if get_jwt().get(ACTOR_CLAIM) != expected:
        # Deliberately the same message whichever way it is wrong: a customer
        # probing admin routes learns only that they may not have them.
        raise PermissionError_("This account cannot perform that action.")


def admin_required(*roles: AdminRole):
    """Require a signed-in, active admin — optionally of a specific role.

    Four checks, in one decorator because doing them separately is how one gets
    forgotten: the token is valid, it is an *admin* token, the account still
    exists and is active, and the role is sufficient. A deactivated admin is
    refused even while holding an unexpired token, so revoking access does not
    wait for the token to lapse.
    """
    allowed: Sequence[str] = [role.value for role in roles]

    def decorator(view):
        @wraps(view)
        @jwt_required()
        def wrapper(*args, **kwargs):
            require_actor(ACTOR_ADMIN)

            admin = db.session.get(Admin, int(get_jwt_identity()))

            if admin is None or not admin.is_active:
                raise PermissionError_("This account is no longer active.")

            if allowed and str(admin.role) not in allowed:
                raise PermissionError_(
                    "Your role does not permit that action.",
                    code="INSUFFICIENT_ROLE",
                )

            g.admin = admin
            return view(*args, **kwargs)

        return wrapper

    return decorator


def customer_required():
    """Require a signed-in, active customer.

    The mirror of ``admin_required``, and separate on purpose: an admin token
    does not authorise customer routes either. The two are different people
    with different data, and "an admin is also every customer" is not a
    property anyone asked for.
    """

    def decorator(view):
        @wraps(view)
        @jwt_required()
        def wrapper(*args, **kwargs):
            require_actor(ACTOR_CUSTOMER)

            customer = db.session.get(Customer, int(get_jwt_identity()))

            if customer is None or not customer.is_active:
                raise PermissionError_("This account is no longer active.")

            g.customer = customer
            return view(*args, **kwargs)

        return wrapper

    return decorator


def current_customer() -> Customer | None:
    """The signed-in customer, or ``None`` — without requiring either.

    For endpoints that work signed in *or* out, checkout above all: an order
    from a signed-in shopper is linked to their account, and the same request
    from a guest is still a perfectly good order. A malformed, expired or
    wrong-actor token is treated as "no account", never as an error, because
    a stale token in someone's browser must not be able to block a sale.
    """
    try:
        verify_jwt_in_request(optional=True)
    except Exception:
        return None

    identity = get_jwt_identity()
    if identity is None or get_jwt().get(ACTOR_CLAIM) != ACTOR_CUSTOMER:
        return None

    customer = db.session.get(Customer, int(identity))
    if customer is None or not customer.is_active:
        return None
    return customer
