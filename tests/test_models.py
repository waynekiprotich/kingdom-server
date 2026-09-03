"""Database-level guarantees behind the business rules (spec §32).

These assert that the *schema* holds the line, not just the service layer —
application checks can be raced, constraints cannot.
"""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.order import OrderStatus, can_transition
from app.models.payment import Payment, PaymentStatus


def test_stock_cannot_go_negative(variant):
    """Business rule 1: no overselling, enforced by CHECK constraint."""
    variant.stock_quantity = -1

    with pytest.raises(IntegrityError):
        db.session.commit()

    db.session.rollback()


def test_a_receipt_number_cannot_be_recorded_twice(order):
    """Business rule 4: a duplicate M-Pesa callback cannot pay an order twice."""
    first = Payment(
        order_id=order.id,
        amount=Decimal("2800.00"),
        phone_number="254712345678",
        status=PaymentStatus.SUCCESSFUL,
        mpesa_receipt_number="RGX1A2B3C4",
    )
    db.session.add(first)
    db.session.commit()

    duplicate = Payment(
        order_id=order.id,
        amount=Decimal("2800.00"),
        phone_number="254712345678",
        status=PaymentStatus.SUCCESSFUL,
        mpesa_receipt_number="RGX1A2B3C4",
    )
    db.session.add(duplicate)

    with pytest.raises(IntegrityError):
        db.session.commit()

    db.session.rollback()


def test_several_payments_may_still_be_awaiting_a_receipt(order):
    """A retried STK push is legitimate while no receipt has arrived."""
    for _ in range(2):
        db.session.add(
            Payment(
                order_id=order.id,
                amount=Decimal("2800.00"),
                phone_number="254712345678",
                status=PaymentStatus.PENDING,
            )
        )
    db.session.commit()

    assert len(order.payments) == 2


def test_a_variant_cannot_duplicate_size_and_colour(variant):
    from app.models.catalog import ProductVariant

    db.session.add(
        ProductVariant(
            product_id=variant.product_id,
            sku="DIFFERENT-SKU",
            size=variant.size,
            color=variant.color,
            price=Decimal("2500.00"),
        )
    )

    with pytest.raises(IntegrityError):
        db.session.commit()

    db.session.rollback()


def test_order_items_require_a_positive_quantity(order, variant):
    from app.models.order import OrderItem

    db.session.add(
        OrderItem(
            order_id=order.id,
            product_variant_id=variant.id,
            product_name="Test Product",
            variant_sku=variant.sku,
            size=variant.size,
            color=variant.color,
            unit_price=Decimal("2500.00"),
            quantity=0,
            line_total=Decimal("0.00"),
        )
    )

    with pytest.raises(IntegrityError):
        db.session.commit()

    db.session.rollback()


def test_an_invalid_status_is_rejected_by_the_database(order):
    """The ORM would catch this; so must the schema.

    SQLAlchemy 2 leaves ``Enum`` columns unconstrained unless asked, which
    would let raw SQL or a stray migration write nonsense into a status.
    """
    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        db.session.execute(
            text("UPDATE orders SET status = 'NOT_A_REAL_STATUS' WHERE id = :id"),
            {"id": order.id},
        )
        db.session.commit()

    db.session.rollback()


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (OrderStatus.PAYMENT_PENDING, OrderStatus.PAID, True),
        (OrderStatus.PAID, OrderStatus.PROCESSING, True),
        (OrderStatus.SHIPPED, OrderStatus.DELIVERED, True),
        # Business rule 7: delivered is terminal.
        (OrderStatus.DELIVERED, OrderStatus.PAYMENT_PENDING, False),
        (OrderStatus.DELIVERED, OrderStatus.PROCESSING, False),
        # Business rule 3: payment cannot be skipped.
        (OrderStatus.CREATED, OrderStatus.PAID, False),
        (OrderStatus.CANCELLED, OrderStatus.PAID, False),
    ],
)
def test_order_status_transitions(current, target, allowed):
    assert can_transition(current, target) is allowed
