"""Inventory movements — an append-only ledger of stock changes (spec §19).

Stock is never silently overwritten: every change is a row here, so
"why is this variant at 3?" always has an answer.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, status_enum


class MovementReason(StrEnum):
    RESTOCK = "restock"
    SALE = "sale"
    CANCELLATION = "cancellation"
    RETURN = "return"
    ADJUSTMENT = "adjustment"


class InventoryMovement(Base, TimestampMixin):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_variant_id: Mapped[int] = mapped_column(
        ForeignKey("product_variants.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: Signed change: negative on sale, positive on restock or cancellation.
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[MovementReason] = mapped_column(
        status_enum(MovementReason, "movement_reason"), nullable=False
    )

    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("delta <> 0", name="ck_inventory_movement_delta_non_zero"),
    )

    def __repr__(self) -> str:
        return f"<InventoryMovement variant={self.product_variant_id} {self.delta:+d}>"
