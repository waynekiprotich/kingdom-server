"""M-Pesa payments (spec §9, §11).

The unique constraint on ``mpesa_receipt_number`` is the enforcement point for
business rule 4: a repeated Daraja callback cannot create a second payment.
Application code checks first, but the database is what makes it true under
concurrent callbacks.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, status_enum
from app.models.order import Order


class PaymentStatus(StrEnum):
    INITIATED = "INITIATED"
    PENDING = "PENDING"
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="mpesa")
    status: Mapped[PaymentStatus] = mapped_column(
        status_enum(PaymentStatus, "payment_status"),
        nullable=False,
        default=PaymentStatus.INITIATED,
        index=True,
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)

    # Returned by the STK push request; the callback is matched on
    # checkout_request_id.
    merchant_request_id: Mapped[str | None] = mapped_column(String(64))
    checkout_request_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # Returned by the callback. Unique — this is the idempotency key.
    mpesa_receipt_number: Mapped[str | None] = mapped_column(
        String(32), unique=True
    )
    result_code: Mapped[int | None] = mapped_column(Integer)
    result_desc: Mapped[str | None] = mapped_column(String(255))
    transaction_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Raw callback body, kept verbatim for dispute resolution (spec §26).
    raw_callback: Mapped[str | None] = mapped_column(Text)

    order: Mapped[Order] = relationship(back_populates="payments")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
    )

    def __repr__(self) -> str:
        return f"<Payment order={self.order_id} {self.status}>"
