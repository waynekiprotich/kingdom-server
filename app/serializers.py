"""Turning models into JSON (spec §13).

Every serializer is an explicit allowlist. Nothing reaches a client unless it
is named here, so adding a column to a model never silently publishes it —
internal fields like ``status``, timestamps, raw stock counts and foreign keys
stay on the server.
"""

from __future__ import annotations

from decimal import Decimal

from flask import current_app

from app.models.catalog import Category, Product, ProductImage, ProductVariant
from app.services import cloudinary

#: Nobody buys forty of one size. Caps the quantity selector independently of
#: how much stock exists, so the number in the UI stays sane.
MAX_QUANTITY_PER_ITEM = 10


def money(value: Decimal | None) -> str | None:
    """Money crosses the wire as a decimal string.

    Floats lose cents, and JSON has no decimal type. The frontend formats these
    for display and never does authoritative arithmetic with them — the backend
    recalculates every total at checkout (§7, §32).
    """
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def serialize_image(image: ProductImage) -> dict:
    widths = tuple(current_app.config["IMAGE_WIDTHS"])

    if image.public_id and cloudinary.is_configured():
        url = cloudinary.build_url(image.public_id, width=widths[-1])
        srcset = cloudinary.build_srcset(image.public_id, widths)
    else:
        # Either the image lives elsewhere, or Cloudinary is not configured in
        # this environment. Serve what we have rather than a broken URL.
        url = image.url
        srcset = None

    return {
        "id": image.id,
        "url": url,
        "srcset": srcset,
        "alt_text": image.alt_text,
        "position": image.position,
        "is_primary": image.is_primary,
    }


def _sorted_images(product: Product) -> list[ProductImage]:
    return sorted(product.images, key=lambda image: (not image.is_primary, image.position))


def primary_image(product: Product) -> dict | None:
    images = _sorted_images(product)
    return serialize_image(images[0]) if images else None


def serialize_variant(variant: ProductVariant) -> dict:
    """Public variant shape.

    ``stock_quantity`` is deliberately absent: exact inventory is business
    information. ``max_quantity`` gives the storefront what it actually needs —
    how many it may offer — without publishing the ledger.
    """
    return {
        "id": variant.id,
        "sku": variant.sku,
        "size": variant.size,
        "color": variant.color,
        "price": money(variant.price),
        "in_stock": variant.in_stock,
        "max_quantity": min(variant.stock_quantity, MAX_QUANTITY_PER_ITEM)
        if variant.in_stock
        else 0,
    }


def serialize_category(category: Category, product_count: int | None = None) -> dict:
    payload = {
        "id": category.id,
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
    }
    if product_count is not None:
        payload["product_count"] = product_count
    return payload


def _price_fields(product: Product) -> dict:
    on_sale = product.sale_price is not None
    return {
        "price": money(product.sale_price if on_sale else product.base_price),
        "base_price": money(product.base_price),
        "sale_price": money(product.sale_price),
        "on_sale": on_sale,
    }


def serialize_product_summary(product: Product, *, in_stock: bool) -> dict:
    """Shape used in grids. No variant list — a 24-product page does not need
    every SKU, and sending them would triple the payload."""
    return {
        "id": product.id,
        "name": product.name,
        "slug": product.slug,
        "featured": product.featured,
        "in_stock": in_stock,
        "image": primary_image(product),
        "category": serialize_category(product.category) if product.category else None,
        **_price_fields(product),
    }


def _timestamps(record) -> dict:
    return {
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


# --- Admin shapes ----------------------------------------------------------
#
# Deliberately separate from the public ones above. The public serializers hide
# status, raw stock and timestamps; reusing them here would either leak those
# to customers or withhold them from the dashboard that needs them.


def serialize_admin_category(category: Category, product_count: int | None = None) -> dict:
    payload = {
        **serialize_category(category, product_count),
        "position": category.position,
        **_timestamps(category),
    }
    return payload


def serialize_admin_variant(variant: ProductVariant) -> dict:
    return {
        "id": variant.id,
        "product_id": variant.product_id,
        "sku": variant.sku,
        "size": variant.size,
        "color": variant.color,
        "price": money(variant.price),
        "stock_quantity": variant.stock_quantity,
        "status": variant.status.value,
        **_timestamps(variant),
    }


def serialize_admin_image(image: ProductImage) -> dict:
    return {
        **serialize_image(image),
        "public_id": image.public_id,
        "source_url": image.url,
    }


def serialize_admin_product_summary(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "slug": product.slug,
        "status": product.status.value,
        "featured": product.featured,
        "category": serialize_category(product.category) if product.category else None,
        "image": primary_image(product),
        "variant_count": len(product.variants),
        "total_stock": sum(variant.stock_quantity for variant in product.variants),
        **_price_fields(product),
        **_timestamps(product),
    }


def serialize_admin_product(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "slug": product.slug,
        "description": product.description,
        "status": product.status.value,
        "featured": product.featured,
        "category": serialize_category(product.category) if product.category else None,
        "category_id": product.category_id,
        "images": [serialize_admin_image(image) for image in _sorted_images(product)],
        "variants": [
            serialize_admin_variant(variant)
            for variant in sorted(product.variants, key=lambda v: (v.size, v.color))
        ],
        **_price_fields(product),
        **_timestamps(product),
    }


def serialize_admin_order_item(item) -> dict:
    return {
        "id": item.id,
        "product_variant_id": item.product_variant_id,
        "product_name": item.product_name,
        "variant_sku": item.variant_sku,
        "size": item.size,
        "color": item.color,
        "unit_price": money(item.unit_price),
        "quantity": item.quantity,
        "line_total": money(item.line_total),
    }


def serialize_admin_payment(payment) -> dict:
    """What an admin may see of a payment.

    Business rule 8: no payment credentials. The raw Daraja callback stays on
    the server — an admin gets the outcome, not the wire traffic.
    """
    return {
        "id": payment.id,
        "provider": payment.provider,
        "status": payment.status.value,
        "amount": money(payment.amount),
        "phone_number": payment.phone_number,
        "mpesa_receipt_number": payment.mpesa_receipt_number,
        "result_desc": payment.result_desc,
        "transaction_date": payment.transaction_date.isoformat()
        if payment.transaction_date
        else None,
        "created_at": payment.created_at.isoformat() if payment.created_at else None,
    }


def serialize_admin_order_summary(order, item_count: int) -> dict:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status.value,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "delivery_location": order.delivery_location,
        "total": money(order.total),
        "item_count": item_count,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
    }


def serialize_admin_order(order) -> dict:
    from app.models.order import ORDER_TRANSITIONS, OrderStatus

    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status.value,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "delivery_location": order.delivery_location,
        "delivery_notes": order.delivery_notes,
        "subtotal": money(order.subtotal),
        "delivery_fee": money(order.delivery_fee),
        "total": money(order.total),
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        "items": [serialize_admin_order_item(item) for item in order.items],
        "payments": [serialize_admin_payment(payment) for payment in order.payments],
        # The transitions an *admin* may perform. The dashboard renders exactly
        # these, so it cannot offer a button the server will refuse.
        #
        # PAID is excluded even though the state machine allows it: only a
        # verified M-Pesa callback may mark an order paid (business rule 3), so
        # offering it to an admin would be a button that always fails.
        "allowed_transitions": sorted(
            status.value
            for status in ORDER_TRANSITIONS.get(order.status, frozenset())
            if status is not OrderStatus.PAID
        ),
        **_timestamps(order),
    }


def serialize_order_item(item) -> dict:
    """Public shape of a line item — same fields as the admin one.

    A separate function anyway: the admin and guest shapes happen to match
    today, but they answer different questions (an admin auditing a sale vs. a
    guest reading their own receipt) and are free to diverge without either
    one silently changing the other.
    """
    return {
        "product_name": item.product_name,
        "variant_sku": item.variant_sku,
        "size": item.size,
        "color": item.color,
        "unit_price": money(item.unit_price),
        "quantity": item.quantity,
        "line_total": money(item.line_total),
    }


#: What a payment status means to the person waiting on it. The storefront
#: shows these rather than composing its own copy from a status code, so the
#: wording stays in one place and stays true to the state machine.
_PAYMENT_MESSAGES = {
    "INITIATED": "Sending the request to M-Pesa…",
    "PENDING": "Check your phone and enter your M-Pesa PIN.",
    "SUCCESSFUL": "Payment received.",
    "FAILED": "The payment did not go through.",
    "CANCELLED": "The payment request was cancelled.",
    "TIMEOUT": "The payment request expired before it was confirmed.",
}


def serialize_payment_status(payment) -> dict:
    """What a guest may see of their own payment.

    Business rule 8 applies here as much as it does to the admin shape: the
    outcome, never the credentials and never the raw Daraja traffic. The
    receipt number is the customer's own and is what they would quote in a
    dispute, so it goes out once the payment has actually succeeded.

    ``result_desc`` is deliberately absent. It is Safaricom's wording, written
    for a developer, and on a failure it is where an unhelpful or confusing
    string would reach a shopper.
    """
    status = payment.status.value
    return {
        "status": status,
        "settled": status in ("SUCCESSFUL", "FAILED", "CANCELLED", "TIMEOUT"),
        "successful": status == "SUCCESSFUL",
        "message": _PAYMENT_MESSAGES.get(status, "Waiting for M-Pesa."),
        "amount": money(payment.amount),
        "receipt": payment.mpesa_receipt_number if status == "SUCCESSFUL" else None,
    }


def serialize_order_confirmation(order) -> dict:
    """What a guest sees on their own order confirmation page.

    Reachable only by ``confirmation_token`` (a guest has no account and no
    other credential), so this can safely include the customer's own contact
    and delivery details — just not anything about payment credentials or
    internal identifiers.
    """
    return {
        "order_number": order.order_number,
        "status": order.status.value,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "delivery_location": order.delivery_location,
        "delivery_notes": order.delivery_notes,
        "subtotal": money(order.subtotal),
        "delivery_fee": money(order.delivery_fee),
        "total": money(order.total),
        "items": [serialize_order_item(item) for item in order.items],
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
    }


def serialize_product_detail(product: Product) -> dict:
    variants = [
        variant
        for variant in sorted(product.variants, key=lambda v: (v.size, v.color))
        if variant.status.value == "active"
    ]
    serialized_variants = [serialize_variant(variant) for variant in variants]

    return {
        "id": product.id,
        "name": product.name,
        "slug": product.slug,
        "description": product.description,
        "featured": product.featured,
        "in_stock": any(variant["in_stock"] for variant in serialized_variants),
        "category": serialize_category(product.category) if product.category else None,
        "images": [serialize_image(image) for image in _sorted_images(product)],
        "variants": serialized_variants,
        # Distinct option values, in the order the storefront should show them.
        "sizes": list(dict.fromkeys(variant["size"] for variant in serialized_variants)),
        "colors": list(dict.fromkeys(variant["color"] for variant in serialized_variants)),
        **_price_fields(product),
    }
