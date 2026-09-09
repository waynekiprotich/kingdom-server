"""Orders and their line items (spec §10, §11)."""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, status_enum


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


#: Legal status moves (spec §10). A delivered order cannot drift back to
#: pending, and a cancelled order is terminal.
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.PAYMENT_PENDING, OrderStatus.CANCELLED}),
    OrderStatus.PAYMENT_PENDING: frozenset(
        {OrderStatus.PAID, OrderStatus.PAYMENT_FAILED, OrderStatus.CANCELLED}
    ),
    OrderStatus.PAYMENT_FAILED: frozenset(
        {OrderStatus.PAYMENT_PENDING, OrderStatus.CANCELLED}
    ),
    OrderStatus.PAID: frozenset({OrderStatus.PROCESSING, OrderStatus.REFUNDED}),
    OrderStatus.PROCESSING: frozenset({OrderStatus.SHIPPED, OrderStatus.REFUNDED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REFUNDED: frozenset(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ORDER_TRANSITIONS.get(current, frozenset())


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True
    )
    #: A random, unguessable identifier for the guest confirmation page —
    #: deliberately not ``order_number``, which is sequential and would let
    #: anyone enumerate other customers' orders by walking KC-100001,
    #: KC-100002, ... A guest has no account, so this token is the only thing
    #: that stands between "the person who just checked out" and "anyone."
    confirmation_token: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        default=lambda: secrets.token_urlsafe(24),
    )
    status: Mapped[OrderStatus] = mapped_column(
        status_enum(OrderStatus, "order_status"),
        nullable=False,
        default=OrderStatus.CREATED,
        index=True,
    )

    #: The account that placed this, when one was signed in. NULL means a
    #: guest order, which stays the primary path — an account is a convenience,
    #: never a requirement to buy.
    #:
    #: RESTRICT, like the catalog rows an order references: order history has
    #: to outlive the account that placed it, so a customer is deactivated,
    #: never deleted out from under their own orders (business rule 9's
    #: reasoning, applied to people).
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )

    # Contact details are still stored on the order itself, account or not.
    # They are a snapshot of where *this* delivery goes: someone who moves
    # house must not have last month's order silently re-addressed, and a
    # guest has nowhere else to keep them (spec §8, §15).
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    delivery_location: Mapped[str] = mapped_column(String(255), nullable=False)
    delivery_notes: Mapped[str | None] = mapped_column(Text)

    # Totals are computed server-side at checkout and stored (spec §7, §32).
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    delivery_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped["Customer | None"] = relationship(  # noqa: F821
        back_populates="orders"
    )
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")  # noqa: F821

    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="ck_orders_subtotal_non_negative"),
        CheckConstraint("delivery_fee >= 0", name="ck_orders_delivery_fee_non_negative"),
        CheckConstraint("total >= 0", name="ck_orders_total_non_negative"),
        Index("ix_orders_status_created_at", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Order {self.order_number} {self.status}>"


class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_variant_id: Mapped[int] = mapped_column(
        ForeignKey("product_variants.id", ondelete="RESTRICT"), nullable=False
    )

    # Snapshot of what was bought, at the price it was bought for. A later
    # price or name change must never rewrite order history (spec §10).
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    variant_sku: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[str] = mapped_column(String(32), nullable=False)
    color: Mapped[str] = mapped_column(String(48), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<OrderItem {self.variant_sku} x{self.quantity}>"
