"""M-Pesa payment endpoints (spec §9, §13).

Three routes, and the interesting thing about them is how differently they are
trusted:

* ``POST /api/orders/<token>/pay`` — a guest asking for a PIN prompt. Reached
  only with the unguessable confirmation token, and rate limited, because it
  makes a phone ring.
* ``GET  /api/orders/<token>/payment`` — the same guest polling for the answer,
  because the callback arrives out of band and the browser has no other way to
  learn about it.
* ``POST /api/payments/mpesa/callback`` — Safaricom. Public and unsigned, since
  Daraja offers no way to authenticate itself, so it is trusted for nothing:
  the ``CheckoutRequestID`` naming a push we initiated is the whole of its
  credential, and the amount is checked against what we asked for.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.errors import ConflictError, NotFoundError, ValidationError
from app.extensions import db
from app.models.order import Order
from app.models.payment import Payment
from app.serializers import serialize_payment_status
from app.services import mpesa, payments as payment_service
from app.services import rate_limit
from app.utils import normalise_kenyan_phone
from app.validation import MISSING, body_str, json_body, reject_unknown_fields

logger = logging.getLogger(__name__)

bp = Blueprint("payments", __name__, url_prefix="/api")


def _order_by_token(token: str) -> Order:
    """The guest's own order, by the token checkout handed them.

    Never by ``order_number`` — see the note in blueprints/checkout.py. The
    same rule applies with more force here, because this endpoint spends money.
    """
    order = db.session.scalar(
        select(Order)
        .where(Order.confirmation_token == token)
        .options(selectinload(Order.items))
    )
    if order is None:
        raise NotFoundError(
            "No order matches that confirmation link.", code="ORDER_NOT_FOUND"
        )
    return order


@bp.post("/orders/<token>/pay")
@rate_limit.limit("payments")
def pay(token: str):
    """Send an M-Pesa PIN prompt for this order."""
    order = _order_by_token(token)

    if not mpesa.is_configured():
        raise ConflictError(
            "M-Pesa is not available just now. Please contact us to complete "
            "your order.",
            code="PAYMENT_UNAVAILABLE",
        )

    body = json_body(required=False)
    reject_unknown_fields(body, ("phone",))

    # Default to the number on the order; a customer may pay from a different
    # handset than the one they want the delivery called to.
    raw_phone = body_str(body, "phone", max_length=20, nullable=True)
    if raw_phone is MISSING or raw_phone is None or not raw_phone.strip():
        phone = order.customer_phone
    else:
        phone = normalise_kenyan_phone(raw_phone)
        if phone is None:
            raise ValidationError(
                "phone must be a valid Kenyan mobile number, e.g. 0712345678."
            )

    payment = payment_service.initiate(order, phone)

    return (
        jsonify({"success": True, "data": {"payment": serialize_payment_status(payment)}}),
        202,
    )


@bp.get("/orders/<token>/payment")
def payment_status(token: str):
    """Where this order's payment has got to.

    The storefront polls this after a push: the callback lands on the server,
    not in the customer's browser, so this is the only way the page finds out.
    """
    order = _order_by_token(token)

    payment = db.session.scalar(
        select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc())
    )

    return jsonify(
        {
            "success": True,
            "data": {
                "order_status": order.status.value,
                "payment": serialize_payment_status(payment) if payment else None,
            },
        }
    )


@bp.post("/payments/mpesa/callback")
def mpesa_callback():
    """Safaricom's result for a push.

    **Always answers 200.** Daraja retries anything else, and a retry loop
    against an endpoint that is already rejecting the callback for a good
    reason (unknown id, wrong amount) achieves nothing but noise. Whether the
    callback was accepted is recorded in the payment row and the log, not in
    the status code — so the body below is an acknowledgement of receipt, never
    a statement that the order is paid.
    """
    body = request.get_json(silent=True)

    try:
        payment_service.apply_callback(body)
    except mpesa.MpesaError as error:
        logger.warning("Unreadable M-Pesa callback: %s", error)
    except Exception:
        # A traceback here must not become a retry storm.
        db.session.rollback()
        logger.exception("Failed to apply an M-Pesa callback.")

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200


@bp.get("/payments/methods")
def methods():
    """Which ways of paying this shop actually offers right now.

    The storefront renders from this rather than hard-coding buttons, so a
    missing WhatsApp number or absent M-Pesa credentials hides the option
    instead of showing one that fails when tapped.
    """
    whatsapp = current_app.config["WHATSAPP_NUMBER"]

    return jsonify(
        {
            "success": True,
            "data": {
                "mpesa": {
                    "available": mpesa.is_configured(),
                    # So the storefront can say so plainly rather than letting a
                    # demo look like a real charge.
                    "simulated": mpesa.is_simulated(),
                },
                "whatsapp": {
                    "available": bool(whatsapp),
                    "number": whatsapp or None,
                },
            },
        }
    )
