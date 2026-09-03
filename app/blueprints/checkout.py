"""Guest checkout (spec §7, §8, §32).

The cart the frontend has been showing is a display snapshot, nothing more.
Every price and every unit of stock is re-read from the database here, inside
one transaction with the rows locked — the only numbers that ever reach an
``Order`` row are the ones this module computed itself (business rules 1, 2).

M-Pesa is not wired up yet (deliberately — it is the next phase). An order
therefore lands in ``PAYMENT_PENDING`` and stops: this is the exact seam the
STK push will plug into later, so nothing here will need to change shape when
it does.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.errors import ConflictError, NotFoundError, ValidationError
from app.extensions import db
from app.models.catalog import ProductStatus, ProductVariant, VariantStatus
from app.models.inventory import InventoryMovement, MovementReason
from app.models.order import Order, OrderItem, OrderStatus
from app.serializers import serialize_order_confirmation
from app.services import rate_limit
from app.utils import generate_order_number, normalise_kenyan_phone
from app.validation import (
    MISSING,
    body_str,
    json_body,
    reject_unknown_fields,
)

bp = Blueprint("checkout", __name__, url_prefix="/api")

#: Same ceiling the storefront already enforces client-side (serializers.py) —
#: repeated here because the frontend's number is a courtesy, not a control.
MAX_QUANTITY_PER_ITEM = 10
MAX_LINE_ITEMS = 30

ORDER_FIELDS = ("items", "customer_name", "customer_phone", "delivery_location", "delivery_notes")


def _parse_items(raw: object) -> list[tuple[int, int]]:
    """Validate the cart payload into ``[(variant_id, quantity), ...]``.

    Deliberately hand-rolled rather than bent through ``body_*`` helpers: this
    is a list of objects, which those were never shaped for, and a bespoke
    dozen lines is exactly the trade CLAUDE.md asks for over a schema library.
    """
    if not isinstance(raw, list) or not raw:
        raise ValidationError("items must be a non-empty list.")
    if len(raw) > MAX_LINE_ITEMS:
        raise ValidationError(f"items cannot contain more than {MAX_LINE_ITEMS} lines.")

    seen: set[int] = set()
    parsed: list[tuple[int, int]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValidationError(f"items[{index}] must be an object.")

        reject_unknown_fields(entry, ("variant_id", "quantity"))

        variant_id = entry.get("variant_id")
        if not isinstance(variant_id, int) or isinstance(variant_id, bool):
            raise ValidationError(f"items[{index}].variant_id must be a whole number.")

        quantity = entry.get("quantity")
        if (
            not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or not (1 <= quantity <= MAX_QUANTITY_PER_ITEM)
        ):
            raise ValidationError(
                f"items[{index}].quantity must be a whole number between 1 and "
                f"{MAX_QUANTITY_PER_ITEM}."
            )

        if variant_id in seen:
            raise ValidationError(
                f"items[{index}].variant_id is duplicated — combine it into one line."
            )
        seen.add(variant_id)
        parsed.append((variant_id, quantity))

    return parsed


@bp.post("/orders")
@rate_limit.limit("orders")
def create_order():
    body = json_body()
    reject_unknown_fields(body, ORDER_FIELDS)

    requested = _parse_items(body.get("items"))

    customer_name = body_str(body, "customer_name", required=True, max_length=120)
    delivery_location = body_str(body, "delivery_location", required=True, max_length=255)
    delivery_notes = body_str(body, "delivery_notes", max_length=1000, nullable=True)
    if delivery_notes is MISSING:
        delivery_notes = None

    raw_phone = body_str(body, "customer_phone", required=True, max_length=20)
    customer_phone = normalise_kenyan_phone(raw_phone)
    if customer_phone is None:
        raise ValidationError(
            "customer_phone must be a valid Kenyan mobile number, e.g. 0712345678."
        )

    variant_ids = [variant_id for variant_id, _ in requested]

    # Locks the rows for the rest of this transaction, so two guests racing
    # for the last unit of the same variant cannot both succeed (business
    # rule 1). SQLite (used for some test runs) does not support FOR UPDATE
    # and silently ignores it — fine there, since sqlite serialises writes
    # at the connection level anyway.
    variants = {
        variant.id: variant
        for variant in db.session.scalars(
            select(ProductVariant)
            .where(ProductVariant.id.in_(variant_ids))
            .options(selectinload(ProductVariant.product))
            .with_for_update()
        )
    }

    subtotal = 0
    order_items: list[OrderItem] = []
    for variant_id, quantity in requested:
        variant = variants.get(variant_id)
        if variant is None:
            raise NotFoundError(
                "One of the items in your cart no longer exists.",
                code="VARIANT_NOT_FOUND",
            )

        product = variant.product
        if product.status != ProductStatus.ACTIVE or variant.status != VariantStatus.ACTIVE:
            raise ConflictError(
                f"{product.name} ({variant.size}, {variant.color}) is no longer available.",
                code="PRODUCT_UNAVAILABLE",
            )

        if variant.stock_quantity < quantity:
            raise ConflictError(
                f"Only {variant.stock_quantity} of {product.name} "
                f"({variant.size}, {variant.color}) left in stock.",
                code="PRODUCT_OUT_OF_STOCK",
            )

        # The price charged is read here, now, from the database — never the
        # figure the cart displayed (business rule 2).
        line_total = variant.price * quantity
        subtotal += line_total

        order_items.append(
            OrderItem(
                product_variant_id=variant.id,
                product_name=product.name,
                variant_sku=variant.sku,
                size=variant.size,
                color=variant.color,
                unit_price=variant.price,
                quantity=quantity,
                line_total=line_total,
            )
        )

    delivery_fee = current_app.config["DELIVERY_FEE"]
    total = subtotal + delivery_fee

    order = Order(
        # Placeholder — replaced below once the row has an id to derive the
        # real order_number from. The column is NOT NULL, so something
        # well-formed has to go here first. confirmation_token is left to the
        # model's own default.
        order_number="PENDING",
        status=OrderStatus.PAYMENT_PENDING,
        customer_name=customer_name,
        customer_phone=customer_phone,
        delivery_location=delivery_location,
        delivery_notes=delivery_notes,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        total=total,
    )
    db.session.add(order)
    db.session.flush()  # assigns order.id

    order.order_number = generate_order_number(order.id)

    for item in order_items:
        item.order_id = order.id
        db.session.add(item)

    for variant_id, quantity in requested:
        variant = variants[variant_id]
        variant.stock_quantity -= quantity
        db.session.add(
            InventoryMovement(
                product_variant_id=variant.id,
                delta=-quantity,
                reason=MovementReason.SALE,
                order_id=order.id,
                note=f"Checkout {order.order_number}",
            )
        )

    db.session.commit()

    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "order": serialize_order_confirmation(order),
                    "confirmation_token": order.confirmation_token,
                },
            }
        ),
        201,
    )


@bp.get("/orders/<token>")
def get_order_confirmation(token: str):
    """A guest's own order, looked up by the unguessable token from checkout.

    Never by ``order_number`` — that is sequential and admin-facing, and
    accepting it here would let anyone walk KC-100001, KC-100002, ... and
    read other customers' names, phone numbers and delivery addresses.
    """
    order = db.session.scalar(
        select(Order)
        .where(Order.confirmation_token == token)
        .options(selectinload(Order.items))
    )
    if order is None:
        raise NotFoundError("No order matches that confirmation link.", code="ORDER_NOT_FOUND")

    return jsonify({"success": True, "data": {"order": serialize_order_confirmation(order)}})
