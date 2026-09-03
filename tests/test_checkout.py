"""Guest checkout (spec §7, §8, §32).

No M-Pesa yet — an order lands in PAYMENT_PENDING and stops. What matters here
is that the server, never the client, decides price and stock (business rules
1 and 2), and that a guest's own order is reachable only through the token
checkout hands back, not through the human-friendly order number.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.catalog import Category, Product, ProductStatus, ProductVariant, VariantStatus
from app.models.inventory import InventoryMovement
from app.models.order import Order


@pytest.fixture
def category(app):
    row = Category(name="Shirts", slug="shirts")
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def product(category):
    row = Product(
        name="Linen Overshirt",
        slug="linen-overshirt",
        category_id=category.id,
        base_price=Decimal("4500.00"),
        status=ProductStatus.ACTIVE,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def variant(product):
    row = ProductVariant(
        product_id=product.id,
        sku="LINEN-M-BON",
        size="M",
        color="Bone",
        price=Decimal("4500.00"),
        stock_quantity=5,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def second_variant(product):
    row = ProductVariant(
        product_id=product.id,
        sku="LINEN-L-BON",
        size="L",
        color="Bone",
        price=Decimal("4700.00"),
        stock_quantity=3,
    )
    db.session.add(row)
    db.session.commit()
    return row


VALID_PAYLOAD = {
    "customer_name": "Achieng Odhiambo",
    "customer_phone": "0712345678",
    "delivery_location": "Kilimani, Nairobi",
    "delivery_notes": "Call on arrival",
}


def checkout(client, variant_id, quantity=1, **overrides):
    payload = {**VALID_PAYLOAD, **overrides, "items": [{"variant_id": variant_id, "quantity": quantity}]}
    return client.post("/api/orders", json=payload)


# --- happy path --------------------------------------------------------


def test_checkout_creates_a_payment_pending_order(client, variant):
    response = checkout(client, variant.id, quantity=2)

    assert response.status_code == 201
    body = response.get_json()["data"]
    order = body["order"]

    assert order["status"] == "PAYMENT_PENDING"
    assert order["subtotal"] == "9000.00"
    assert order["delivery_fee"] == "300.00"
    assert order["total"] == "9300.00"
    assert order["items"] == [
        {
            "product_name": "Linen Overshirt",
            "variant_sku": "LINEN-M-BON",
            "size": "M",
            "color": "Bone",
            "unit_price": "4500.00",
            "quantity": 2,
            "line_total": "9000.00",
        }
    ]
    assert order["order_number"].startswith("KC-")
    assert "confirmation_token" in body


def test_checkout_decrements_stock_through_the_ledger(client, variant):
    checkout(client, variant.id, quantity=2)

    db.session.refresh(variant)
    assert variant.stock_quantity == 3

    movement = db.session.scalar(
        select(InventoryMovement).where(InventoryMovement.product_variant_id == variant.id)
    )
    assert movement.delta == -2
    assert movement.reason.value == "sale"
    assert movement.order_id is not None


def test_checkout_recalculates_price_from_the_database(client, variant):
    """The cart cannot send its own price — there is nowhere to put one."""
    payload = {
        **VALID_PAYLOAD,
        "items": [{"variant_id": variant.id, "quantity": 1, "unit_price": "1.00"}],
    }
    response = client.post("/api/orders", json=payload)

    assert response.status_code == 422


def test_checkout_supports_multiple_lines(client, variant, second_variant):
    payload = {
        **VALID_PAYLOAD,
        "items": [
            {"variant_id": variant.id, "quantity": 1},
            {"variant_id": second_variant.id, "quantity": 2},
        ],
    }
    response = client.post("/api/orders", json=payload)

    assert response.status_code == 201
    order = response.get_json()["data"]["order"]
    # 4500 + (4700 * 2) = 13900, + 300 delivery
    assert order["subtotal"] == "13900.00"
    assert order["total"] == "14200.00"
    assert len(order["items"]) == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0712345678", "254712345678"),
        ("712345678", "254712345678"),
        ("+254712345678", "254712345678"),
        ("254712345678", "254712345678"),
        ("0112345678", "254112345678"),
        ("0722 345 678", "254722345678"),
    ],
)
def test_phone_number_formats_are_normalised(client, variant, raw, expected):
    response = checkout(client, variant.id, customer_phone=raw)

    assert response.status_code == 201
    saved = db.session.scalar(select(Order))
    assert saved.customer_phone == expected


@pytest.mark.parametrize("bad", ["12345", "0812345678", "abcdefghij", "+1 555 0100"])
def test_implausible_phone_numbers_are_rejected(client, variant, bad):
    response = checkout(client, variant.id, customer_phone=bad)

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


# --- validation ----------------------------------------------------------


def test_empty_cart_is_rejected(client):
    payload = {**VALID_PAYLOAD, "items": []}
    assert client.post("/api/orders", json=payload).status_code == 422


def test_missing_customer_name_is_rejected(client, variant):
    payload = {**VALID_PAYLOAD, "items": [{"variant_id": variant.id, "quantity": 1}]}
    del payload["customer_name"]
    assert client.post("/api/orders", json=payload).status_code == 422


def test_unknown_top_level_field_is_rejected(client, variant):
    payload = {
        **VALID_PAYLOAD,
        "items": [{"variant_id": variant.id, "quantity": 1}],
        "discount_code": "FREESTUFF",
    }
    assert client.post("/api/orders", json=payload).status_code == 422


def test_quantity_over_the_line_cap_is_rejected(client, variant):
    response = checkout(client, variant.id, quantity=11)
    assert response.status_code == 422


def test_duplicate_variant_in_cart_is_rejected(client, variant):
    payload = {
        **VALID_PAYLOAD,
        "items": [
            {"variant_id": variant.id, "quantity": 1},
            {"variant_id": variant.id, "quantity": 1},
        ],
    }
    assert client.post("/api/orders", json=payload).status_code == 422


def test_delivery_notes_are_optional(client, variant):
    payload = {**VALID_PAYLOAD, "items": [{"variant_id": variant.id, "quantity": 1}]}
    del payload["delivery_notes"]
    response = client.post("/api/orders", json=payload)

    assert response.status_code == 201
    assert response.get_json()["data"]["order"]["delivery_notes"] is None


# --- stock and availability ------------------------------------------------


def test_unknown_variant_is_a_404(client):
    response = checkout(client, 999_999)

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "VARIANT_NOT_FOUND"


def test_insufficient_stock_is_refused(client, variant):
    # variant has 5 in stock; 6 is within the per-line cap but not in stock.
    response = checkout(client, variant.id, quantity=6)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PRODUCT_OUT_OF_STOCK"

    db.session.refresh(variant)
    assert variant.stock_quantity == 5  # untouched


def test_out_of_stock_variant_is_refused(client, variant):
    variant.stock_quantity = 0
    db.session.commit()

    response = checkout(client, variant.id, quantity=1)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PRODUCT_OUT_OF_STOCK"


def test_inactive_variant_is_refused(client, variant):
    variant.status = VariantStatus.INACTIVE
    db.session.commit()

    response = checkout(client, variant.id, quantity=1)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PRODUCT_UNAVAILABLE"


def test_draft_product_is_refused(client, product, variant):
    product.status = ProductStatus.DRAFT
    db.session.commit()

    response = checkout(client, variant.id, quantity=1)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PRODUCT_UNAVAILABLE"


def test_archived_product_is_refused(client, product, variant):
    product.status = ProductStatus.ARCHIVED
    db.session.commit()

    response = checkout(client, variant.id, quantity=1)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PRODUCT_UNAVAILABLE"


# --- confirmation lookup ----------------------------------------------------


def test_confirmation_token_reaches_the_full_order(client, variant):
    created = checkout(client, variant.id, quantity=1).get_json()["data"]
    token = created["confirmation_token"]

    response = client.get(f"/api/orders/{token}")

    assert response.status_code == 200
    order = response.get_json()["data"]["order"]
    assert order["order_number"] == created["order"]["order_number"]
    assert order["customer_name"] == "Achieng Odhiambo"


def test_order_number_does_not_work_as_a_lookup_key(client, variant):
    """Business rule: a guest's order is not enumerable by anyone who can
    guess the next sequential KC-100xxx number."""
    created = checkout(client, variant.id, quantity=1).get_json()["data"]
    order_number = created["order"]["order_number"]

    response = client.get(f"/api/orders/{order_number}")

    assert response.status_code == 404


def test_unknown_token_is_a_404(client):
    response = client.get("/api/orders/not-a-real-token")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_two_checkouts_get_distinct_tokens_and_order_numbers(client, variant, second_variant):
    first = checkout(client, variant.id, quantity=1).get_json()["data"]
    second = checkout(client, second_variant.id, quantity=1).get_json()["data"]

    assert first["confirmation_token"] != second["confirmation_token"]
    assert first["order"]["order_number"] != second["order"]["order_number"]
