"""Admin order management (spec §10, §13, §19).

Status changes go through ``ORDER_TRANSITIONS`` rather than accepting whatever
the client sends, so business rule 7 — a delivered order cannot drift back to
pending — is enforced here and not left to the dashboard to remember.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.authz import admin_required
from app.errors import ConflictError, NotFoundError, ValidationError
from app.extensions import db
from app.models.catalog import ProductVariant
from app.models.inventory import InventoryMovement, MovementReason
from app.models.order import (
    ORDER_TRANSITIONS,
    Order,
    OrderItem,
    OrderStatus,
    can_transition,
)
from app.models.base import utcnow
from app.serializers import serialize_admin_order, serialize_admin_order_summary
from app.services import audit
from app.validation import body_choice, json_body, query_int, query_str, reject_unknown_fields

bp = Blueprint("admin_orders", __name__, url_prefix="/api/admin/orders")

ORDER_STATUSES = tuple(status.value for status in OrderStatus)


def _get_order(order_id: int) -> Order:
    order = db.session.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items), selectinload(Order.payments))
    )
    if order is None:
        raise NotFoundError(f"No order with id {order_id}.", code="ORDER_NOT_FOUND")
    return order


def _release_inventory(order: Order) -> None:
    """Return a cancelled order's stock to the shelf (business rule 6).

    Every unit goes back through the ledger, so the movements still sum to the
    quantity on hand.
    """
    for item in order.items:
        variant = db.session.get(ProductVariant, item.product_variant_id)
        if variant is None:
            continue

        variant.stock_quantity += item.quantity
        db.session.add(
            InventoryMovement(
                product_variant_id=variant.id,
                delta=item.quantity,
                reason=MovementReason.CANCELLATION,
                order_id=order.id,
                admin_id=g.admin.id,
                note=f"Released by cancelling {order.order_number}",
            )
        )


@bp.get("")
@admin_required()
def list_orders():
    page = query_int("page", default=1, minimum=1, maximum=10_000)
    per_page = query_int("per_page", default=25, minimum=1, maximum=100)
    status = query_str("status", max_length=20)
    search = query_str("q")

    filters = []
    if status:
        if status not in ORDER_STATUSES:
            raise ValidationError(f"status must be one of: {', '.join(ORDER_STATUSES)}.")
        filters.append(Order.status == OrderStatus(status))
    if search:
        term = f"%{search}%"
        filters.append(
            or_(
                Order.order_number.ilike(term),
                Order.customer_name.ilike(term),
                Order.customer_phone.ilike(term),
            )
        )

    total = db.session.scalar(select(func.count()).select_from(Order).where(*filters))

    # Not labelled "items": ColumnCollection already has an .items() method,
    # and `.c.items` would silently resolve to that method rather than the
    # column, producing an unusable query.
    item_counts = (
        select(OrderItem.order_id, func.count(OrderItem.id).label("line_count"))
        .group_by(OrderItem.order_id)
        .subquery()
    )
    rows = db.session.execute(
        select(Order, func.coalesce(item_counts.c.line_count, 0))
        .outerjoin(item_counts, item_counts.c.order_id == Order.id)
        .where(*filters)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    total_pages = (total + per_page - 1) // per_page if total else 0

    return jsonify(
        {
            "success": True,
            "data": {
                "orders": [
                    serialize_admin_order_summary(order, item_count)
                    for order, item_count in rows
                ],
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "total_pages": total_pages,
                    "has_prev": page > 1,
                    "has_next": page < total_pages,
                },
                "status_counts": {
                    row.status.value: row.total
                    for row in db.session.execute(
                        select(Order.status, func.count(Order.id).label("total")).group_by(
                            Order.status
                        )
                    ).all()
                },
            },
        }
    )


@bp.get("/<int:order_id>")
@admin_required()
def get_order(order_id: int):
    return jsonify(
        {"success": True, "data": {"order": serialize_admin_order(_get_order(order_id))}}
    )


@bp.patch("/<int:order_id>/status")
@admin_required()
def update_status(order_id: int):
    order = _get_order(order_id)
    body = json_body()
    reject_unknown_fields(body, ("status",))

    target = OrderStatus(body_choice(body, "status", ORDER_STATUSES, required=True))

    if target == order.status:
        return jsonify({"success": True, "data": {"order": serialize_admin_order(order)}})

    if not can_transition(order.status, target):
        allowed = sorted(
            status.value for status in ORDER_TRANSITIONS.get(order.status, frozenset())
        )
        raise ConflictError(
            f"An order that is {order.status.value} cannot become {target.value}."
            + (f" It can only become: {', '.join(allowed)}." if allowed else " It is final."),
            code="INVALID_STATUS_TRANSITION",
        )

    # An admin never marks an order paid by hand: only a verified M-Pesa
    # callback may do that (business rule 3).
    if target == OrderStatus.PAID:
        raise ConflictError(
            "An order becomes paid only through a verified M-Pesa callback.",
            code="PAYMENT_NOT_VERIFIED",
        )

    previous = order.status
    order.status = target

    if target == OrderStatus.CANCELLED:
        order.cancelled_at = utcnow()
        _release_inventory(order)

    audit.record(
        "order.status",
        "order",
        order.id,
        changes={"status": {"from": previous.value, "to": target.value}},
    )
    db.session.commit()

    return jsonify({"success": True, "data": {"order": serialize_admin_order(order)}})
