"""Model package.

Everything is imported here so ``Base.metadata`` is complete by the time
Alembic autogenerates a migration — a model that is never imported is a table
that never gets created.
"""

from app.models.admin import Admin, AdminRole
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.catalog import (
    Category,
    Product,
    ProductImage,
    ProductStatus,
    ProductVariant,
    VariantStatus,
)
from app.models.inventory import InventoryMovement, MovementReason
from app.models.order import (
    ORDER_TRANSITIONS,
    Order,
    OrderItem,
    OrderStatus,
    can_transition,
)
from app.models.payment import Payment, PaymentStatus

__all__ = [
    "ORDER_TRANSITIONS",
    "Admin",
    "AdminRole",
    "AuditLog",
    "Base",
    "Category",
    "InventoryMovement",
    "MovementReason",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Payment",
    "PaymentStatus",
    "Product",
    "ProductImage",
    "ProductStatus",
    "ProductVariant",
    "VariantStatus",
    "can_transition",
]
