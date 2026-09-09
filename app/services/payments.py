"""Turning an M-Pesa result into a paid order (spec §9, §24).

This is the only module allowed to move an order to ``PAID``, and it does it in
exactly one function, so business rules 3 and 4 have one place to hold:

* **Rule 3** — an order becomes ``PAID`` only from a callback that Safaricom
  sent, for a push *we* initiated, for the amount we asked for. There is no
  admin action and no client call that can substitute for that; the admin
  endpoint refuses ``PAID`` outright.
* **Rule 4** — a repeated callback cannot create a second payment. The unique
  constraint on ``payments.mpesa_receipt_number`` is what makes that true; the
  check below is the courtesy that turns a constraint violation into a quiet
  no-op instead of a 500.

The simulator goes through ``apply_callback`` too. It builds a Daraja envelope
and hands it to the same parser and the same function, so there is no second
path that only the demo exercises — and nothing to forget to re-test when the
real credentials arrive.
"""

from __future__ import annotations

import logging
import secrets
import threading
from decimal import ROUND_CEILING, Decimal

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.errors import ConflictError
from app.extensions import db
from app.models.order import Order, OrderStatus, can_transition
from app.models.payment import Payment, PaymentStatus
from app.models.base import utcnow
from app.services import mpesa

logger = logging.getLogger(__name__)

#: Statuses a push is still outstanding in. A second push while one of these is
#: open would put a second prompt on the customer's handset for the same order.
OPEN_STATUSES = (PaymentStatus.INITIATED, PaymentStatus.PENDING)


def stk_amount(total: Decimal) -> int:
    """Whole shillings, rounded up.

    Daraja rejects a decimal ``Amount``. Rounding *up* rather than to nearest so
    that a total with cents in it can never charge the customer less than the
    order says — undercharging is a loss the shop cannot recover, and the
    difference is at most one shilling.
    """
    return int(Decimal(total).quantize(Decimal("1"), rounding=ROUND_CEILING))


def open_payment_for(order: Order) -> Payment | None:
    """The push already outstanding on this order, if there is one."""
    return db.session.scalar(
        select(Payment)
        .where(Payment.order_id == order.id, Payment.status.in_(OPEN_STATUSES))
        .order_by(Payment.id.desc())
    )


def initiate(order: Order, phone: str) -> Payment:
    """Put a PIN prompt on ``phone`` for ``order`` and record that we did.

    Returns the ``Payment`` row representing the attempt. The order is *not*
    paid at this point and must not be treated as such — acceptance by Daraja
    only means a prompt is on its way.
    """
    if order.status == OrderStatus.PAID:
        raise ConflictError(
            "That order has already been paid.", code="ORDER_ALREADY_PAID"
        )

    # A previous attempt was declined, cancelled or timed out. The state
    # machine has a move for precisely this — PAYMENT_FAILED back to
    # PAYMENT_PENDING — so take it, rather than reading a declined payment as
    # the end of the sale. Without this, one mistyped PIN would strand an order
    # that still has stock reserved against it.
    if order.status == OrderStatus.PAYMENT_FAILED:
        order.status = OrderStatus.PAYMENT_PENDING

    if not can_transition(order.status, OrderStatus.PAID):
        raise ConflictError(
            f"An order that is {order.status.value.lower().replace('_', ' ')} "
            "cannot be paid.",
            code="ORDER_NOT_PAYABLE",
        )

    # One prompt at a time. A customer who taps "Pay" twice should not get two
    # PIN requests, and this is what stops it — the rate limit is a blunter
    # instrument aimed at scripts, not at an impatient shopper.
    existing = open_payment_for(order)
    if existing is not None:
        raise ConflictError(
            "A payment request for this order is already waiting on your phone. "
            "Enter your M-Pesa PIN, or wait a moment and try again.",
            code="PAYMENT_ALREADY_PENDING",
        )

    amount = stk_amount(order.total)

    payment = Payment(
        order_id=order.id,
        provider="mpesa",
        status=PaymentStatus.INITIATED,
        amount=Decimal(amount),
        phone_number=phone,
    )
    db.session.add(payment)
    db.session.flush()

    if mpesa.is_simulated():
        payment.merchant_request_id = f"SIM-{secrets.token_hex(6).upper()}"
        payment.checkout_request_id = f"ws_CO_SIM_{secrets.token_hex(8).upper()}"
        payment.status = PaymentStatus.PENDING
        payment.result_desc = "Simulated push. No prompt was sent to the handset."
        db.session.commit()

        _schedule_simulated_callback(payment)
        return payment

    try:
        pushed = mpesa.stk_push(
            phone=phone,
            amount=amount,
            reference=order.order_number,
            description=f"Order {order.order_number}"[:13],
        )
    except mpesa.MpesaError as error:
        # Record the failed attempt rather than losing it: "I tried to pay and
        # nothing happened" needs to leave a trace, and without one the order
        # looks like the customer never tried.
        payment.status = PaymentStatus.FAILED
        payment.result_desc = str(error)[:255]
        db.session.commit()
        logger.warning(
            "STK push failed for order %s: %s", order.order_number, error
        )
        raise ConflictError(
            "We could not reach M-Pesa just now. Please try again in a moment.",
            code="PAYMENT_INITIATION_FAILED",
        ) from error

    payment.merchant_request_id = pushed.merchant_request_id
    payment.checkout_request_id = pushed.checkout_request_id
    payment.status = PaymentStatus.PENDING
    payment.result_desc = pushed.customer_message[:255]
    db.session.commit()

    return payment


def apply_callback(body: object) -> Payment | None:
    """Apply a Daraja callback. The only route to ``PAID``.

    Returns the payment it touched, or ``None`` when the callback did not match
    anything we initiated. Never raises for a callback that is merely wrong —
    the endpoint has to answer 200 either way or Safaricom retries forever — so
    every rejection is logged instead.
    """
    result = mpesa.parse_callback(body)

    # This is the authentication. The endpoint is public and unsigned, so the
    # only thing distinguishing a real callback from an invented one is that it
    # names a CheckoutRequestID we asked Safaricom for.
    payment = db.session.scalar(
        select(Payment).where(Payment.checkout_request_id == result.checkout_request_id)
    )
    if payment is None:
        logger.warning(
            "M-Pesa callback for unknown CheckoutRequestID %s — ignoring.",
            result.checkout_request_id,
        )
        return None

    # Rule 4, checked before writing. The unique index is still what enforces
    # it under two callbacks arriving at once; this just makes the ordinary
    # repeat quiet.
    if payment.status == PaymentStatus.SUCCESSFUL:
        logger.info(
            "Duplicate M-Pesa callback for %s (receipt %s) — already applied.",
            payment.checkout_request_id,
            payment.mpesa_receipt_number,
        )
        return payment

    payment.raw_callback = _raw(body)
    payment.result_code = result.result_code
    payment.result_desc = result.result_desc or ""

    if not result.successful:
        payment.status = _failure_status(result.result_code)
        order = db.session.get(Order, payment.order_id)
        if order is not None and can_transition(order.status, OrderStatus.PAYMENT_FAILED):
            order.status = OrderStatus.PAYMENT_FAILED
        db.session.commit()
        logger.info(
            "M-Pesa payment %s failed: %s (%s)",
            payment.id,
            result.result_desc,
            result.result_code,
        )
        return payment

    order = db.session.get(Order, payment.order_id)
    if order is None:  # pragma: no cover - a payment cannot outlive its order
        logger.error("Payment %s references a missing order.", payment.id)
        return payment

    # A successful callback that paid the wrong amount is not a paid order.
    # Safaricom has taken the customer's money, so this is recorded and
    # escalated rather than silently dropped — it needs a human.
    if result.amount is None or result.amount != int(payment.amount):
        payment.status = PaymentStatus.FAILED
        payment.mpesa_receipt_number = result.receipt
        payment.result_desc = (
            f"Amount mismatch: expected {int(payment.amount)}, received "
            f"{result.amount}."
        )[:255]
        db.session.commit()
        logger.error(
            "M-Pesa amount mismatch on order %s: expected %s, received %s, "
            "receipt %s. Needs manual reconciliation.",
            order.order_number,
            int(payment.amount),
            result.amount,
            result.receipt,
        )
        return payment

    payment.status = PaymentStatus.SUCCESSFUL
    payment.mpesa_receipt_number = result.receipt
    payment.transaction_date = result.transaction_date
    if result.phone_number:
        payment.phone_number = result.phone_number

    # Rule 3, and the only place it happens.
    if can_transition(order.status, OrderStatus.PAID):
        order.status = OrderStatus.PAID
        order.paid_at = utcnow()
    else:
        logger.warning(
            "Order %s was %s when a successful payment arrived; recording the "
            "payment but leaving the status alone.",
            order.order_number,
            order.status.value,
        )

    try:
        db.session.commit()
    except IntegrityError:
        # Two callbacks for the same receipt raced. The constraint did its job;
        # the other one won and the order is paid either way.
        db.session.rollback()
        logger.info(
            "Concurrent duplicate callback for receipt %s — the unique "
            "constraint rejected this one.",
            result.receipt,
        )
        return db.session.scalar(
            select(Payment).where(
                Payment.checkout_request_id == result.checkout_request_id
            )
        )

    logger.info(
        "Order %s paid: receipt %s, KES %s.",
        order.order_number,
        result.receipt,
        int(payment.amount),
    )
    return payment


def _failure_status(result_code: int) -> PaymentStatus:
    """Map Daraja's result codes onto our own vocabulary.

    Only the two that a customer would describe differently are distinguished;
    everything else is a failure, because inventing a status per code would be
    a list to maintain against a service that adds to it.
    """
    if result_code == 1032:  # request cancelled by user
        return PaymentStatus.CANCELLED
    if result_code == 1037:  # timeout waiting for the user
        return PaymentStatus.TIMEOUT
    return PaymentStatus.FAILED


def _raw(body: object) -> str:
    """The callback as received, for dispute resolution (spec §26)."""
    import json

    try:
        return json.dumps(body)[:10_000]
    except (TypeError, ValueError):
        return str(body)[:10_000]


def _schedule_simulated_callback(payment: Payment) -> None:
    """Deliver a synthetic success through the real handler, shortly.

    Development only — ``ProductionConfig`` refuses to start in simulator mode,
    so this cannot run on a live shop. The delay exists so the storefront's
    "check your phone" state is actually visible instead of flashing past.
    """
    app = current_app._get_current_object()
    if not app.config["MPESA_SIMULATOR_AUTO_CALLBACK"]:
        return

    delay = app.config["MPESA_SIMULATOR_DELAY_SECONDS"]

    checkout_request_id = payment.checkout_request_id
    merchant_request_id = payment.merchant_request_id
    amount = int(payment.amount)
    phone = payment.phone_number
    receipt = f"S{secrets.token_hex(4).upper()}"

    def deliver() -> None:
        with app.app_context():
            try:
                apply_callback(
                    mpesa.simulated_callback(
                        checkout_request_id=checkout_request_id,
                        merchant_request_id=merchant_request_id,
                        amount=amount,
                        phone=phone,
                        receipt=receipt,
                    )
                )
            except Exception:  # pragma: no cover - a demo must not crash a worker
                logger.exception("Simulated M-Pesa callback failed.")
            finally:
                db.session.remove()

    timer = threading.Timer(delay, deliver)
    timer.daemon = True
    timer.start()
