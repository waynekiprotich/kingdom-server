"""Admin view of customer accounts (spec §13, §19).

Read-mostly on purpose. The only mutation is activating or deactivating an
account, because that is the only one running a shop actually needs: an admin
has no business editing someone's details behind their back, and there is
deliberately no way to read, reset or set a customer's password from here.

Accounts are deactivated, never deleted — an order has to keep pointing at the
person who placed it (the reasoning behind business rule 9, applied to people).
"""

from __future__ import annotations

from flask import Blueprint, jsonify
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.authz import admin_required
from app.errors import NotFoundError
from app.extensions import db
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.serializers import (
    serialize_admin_customer,
    serialize_admin_order_summary,
)
from app.services import audit
from app.validation import (
    body_bool,
    json_body,
    like_pattern,
    query_int,
    query_str,
    reject_unknown_fields,
)

bp = Blueprint("admin_customers", __name__, url_prefix="/api/admin/customers")

#: Statuses that represent money actually taken. "Total spent" counting
#: cancelled and unpaid orders would flatter every customer and mislead every
#: decision made from it.
SETTLED_STATUSES = (
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
)


def _order_stats_columns():
    """Per-customer order count and lifetime spend, correlated to the page.

    Correlated for the same reason as the order listing's line count: a
    standalone GROUP BY over every order ever placed, joined in, makes the cost
    of this page grow with total sales rather than with the twenty-five rows on
    it. See CLAUDE.md, "Count rows on the page, not in the table."
    """
    order_count = (
        select(func.count(Order.id))
        .where(Order.customer_id == Customer.id)
        .correlate(Customer)
        .scalar_subquery()
        .label("order_count")
    )
    total_spent = (
        select(func.coalesce(func.sum(Order.total), 0))
        .where(Order.customer_id == Customer.id, Order.status.in_(SETTLED_STATUSES))
        .correlate(Customer)
        .scalar_subquery()
        .label("total_spent")
    )
    return order_count, total_spent


@bp.get("")
@admin_required()
def list_customers():
    page = query_int("page", default=1, minimum=1, maximum=10_000)
    per_page = query_int("per_page", default=25, minimum=1, maximum=100)
    search = query_str("q")

    filters = []
    if search:
        term = like_pattern(search)
        filters.append(
            or_(
                Customer.name.ilike(term, escape="\\"),
                Customer.email.ilike(term, escape="\\"),
                Customer.phone.ilike(term, escape="\\"),
            )
        )

    total = db.session.scalar(
        select(func.count()).select_from(Customer).where(*filters)
    )

    order_count, total_spent = _order_stats_columns()
    rows = db.session.execute(
        select(Customer, order_count, total_spent)
        .where(*filters)
        .order_by(Customer.created_at.desc(), Customer.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    total_pages = (total + per_page - 1) // per_page if total else 0

    return jsonify(
        {
            "success": True,
            "data": {
                "customers": [
                    serialize_admin_customer(customer, count, spent)
                    for customer, count, spent in rows
                ],
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "total_pages": total_pages,
                    "has_prev": page > 1,
                    "has_next": page < total_pages,
                },
            },
        }
    )


def _get_customer(customer_id: int) -> Customer:
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError(
            f"No customer with id {customer_id}.", code="CUSTOMER_NOT_FOUND"
        )
    return customer


@bp.get("/<int:customer_id>")
@admin_required()
def get_customer(customer_id: int):
    customer = _get_customer(customer_id)

    orders = db.session.scalars(
        select(Order)
        .where(Order.customer_id == customer.id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .options(selectinload(Order.items))
    ).all()

    count = len(orders)
    spent = sum(
        (order.total for order in orders if order.status in SETTLED_STATUSES),
        start=0,
    )

    return jsonify(
        {
            "success": True,
            "data": {
                "customer": serialize_admin_customer(customer, count, spent),
                "orders": [
                    serialize_admin_order_summary(order, len(order.items))
                    for order in orders
                ],
            },
        }
    )


@bp.patch("/<int:customer_id>")
@admin_required()
def update_customer(customer_id: int):
    """Activate or deactivate an account. Nothing else is editable here."""
    customer = _get_customer(customer_id)
    body = json_body()
    reject_unknown_fields(body, ("is_active",))

    is_active = body_bool(body, "is_active", required=True)
    if is_active == customer.is_active:
        return jsonify(
            {"success": True, "data": {"customer": serialize_admin_customer(customer)}}
        )

    previous = customer.is_active
    customer.is_active = is_active

    audit.record(
        "customer.is_active",
        "customer",
        customer.id,
        changes={"is_active": {"from": previous, "to": is_active}},
    )
    db.session.commit()

    return jsonify(
        {"success": True, "data": {"customer": serialize_admin_customer(customer)}}
    )
