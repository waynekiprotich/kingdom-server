"""Admin order management (spec §10, §13, §19)."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.audit import AuditLog
from app.models.catalog import Category, Product, ProductStatus, ProductVariant
from app.models.inventory import InventoryMovement
from app.models.order import Order, OrderItem, OrderStatus
from app.models.payment import Payment, PaymentStatus


@pytest.fixture
def stocked_variant(app):
    category = Category(name="Shirts", slug="shirts")
    db.session.add(category)
    db.session.flush()

    product = Product(
        name="Linen Overshirt",
        slug="linen-overshirt",
        category_id=category.id,
        base_price=Decimal("4500.00"),
        status=ProductStatus.ACTIVE,
    )
    db.session.add(product)
    db.session.flush()

    variant = ProductVariant(
        product_id=product.id,
        sku="LINEN-M-BON",
        size="M",
        color="Bone",
        price=Decimal("4500.00"),
        stock_quantity=5,
    )
    db.session.add(variant)
    db.session.commit()
    return variant


def make_order(variant, *, number, status, quantity=1, name="Achieng Odhiambo"):
    line_total = variant.price * quantity
    order = Order(
        order_number=number,
        status=status,
        customer_name=name,
        customer_phone="254712000001",
        delivery_location="Kilimani, Nairobi",
        subtotal=line_total,
        delivery_fee=Decimal("300.00"),
        total=line_total + Decimal("300.00"),
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_variant_id=variant.id,
            product_name="Linen Overshirt",
            variant_sku=variant.sku,
            size=variant.size,
            color=variant.color,
            unit_price=variant.price,
            quantity=quantity,
            line_total=line_total,
        )
    )
    db.session.commit()
    return order


@pytest.fixture
def order(stocked_variant):
    return make_order(stocked_variant, number="KC-100001", status=OrderStatus.PROCESSING)


# --- access ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/admin/orders"), ("get", "/api/admin/orders/1"), ("patch", "/api/admin/orders/1/status")],
)
def test_order_endpoints_require_a_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_unknown_order_is_a_404(client, auth_headers):
    response = client.get("/api/admin/orders/99999", headers=auth_headers)

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "ORDER_NOT_FOUND"


# --- listing ---------------------------------------------------------------


def test_listing_returns_orders_with_item_counts(client, auth_headers, order):
    data = client.get("/api/admin/orders", headers=auth_headers).get_json()["data"]

    assert data["pagination"]["total"] == 1
    assert data["orders"][0]["order_number"] == "KC-100001"
    assert data["orders"][0]["item_count"] == 1


def test_listing_reports_counts_per_status(client, auth_headers, stocked_variant):
    make_order(stocked_variant, number="KC-1", status=OrderStatus.PAID)
    make_order(stocked_variant, number="KC-2", status=OrderStatus.PAID)
    make_order(stocked_variant, number="KC-3", status=OrderStatus.SHIPPED)

    counts = client.get("/api/admin/orders", headers=auth_headers).get_json()["data"][
        "status_counts"
    ]

    assert counts == {"PAID": 2, "SHIPPED": 1}


def test_listing_filters_by_status(client, auth_headers, stocked_variant):
    make_order(stocked_variant, number="KC-1", status=OrderStatus.PAID)
    make_order(stocked_variant, number="KC-2", status=OrderStatus.SHIPPED)

    found = client.get("/api/admin/orders?status=SHIPPED", headers=auth_headers).get_json()[
        "data"
    ]["orders"]

    assert [o["order_number"] for o in found] == ["KC-2"]


def test_listing_rejects_an_unknown_status(client, auth_headers):
    assert client.get("/api/admin/orders?status=NOPE", headers=auth_headers).status_code == 422


@pytest.mark.parametrize("term", ["KC-100001", "Achieng", "254712000001"])
def test_search_matches_number_name_and_phone(client, auth_headers, order, term):
    found = client.get(f"/api/admin/orders?q={term}", headers=auth_headers).get_json()[
        "data"
    ]["orders"]

    assert len(found) == 1


def test_listing_paginates(client, auth_headers, stocked_variant):
    for index in range(3):
        make_order(stocked_variant, number=f"KC-{index}", status=OrderStatus.PAID)

    page = client.get("/api/admin/orders?per_page=2&page=2", headers=auth_headers).get_json()[
        "data"
    ]

    assert len(page["orders"]) == 1
    assert page["pagination"]["has_next"] is False


# --- detail ----------------------------------------------------------------


def test_detail_includes_items_and_legal_next_states(client, auth_headers, order):
    detail = client.get(f"/api/admin/orders/{order.id}", headers=auth_headers).get_json()[
        "data"
    ]["order"]

    assert detail["items"][0]["variant_sku"] == "LINEN-M-BON"
    assert detail["total"] == "4800.00"
    # PROCESSING may only ship or be refunded.
    assert detail["allowed_transitions"] == ["REFUNDED", "SHIPPED"]


def test_paid_is_never_offered_as_an_admin_action(client, auth_headers, stocked_variant):
    """PAID is a legal state change but not an admin one (business rule 3).

    Offering it would be a button that always fails.
    """
    pending = make_order(
        stocked_variant, number="KC-P", status=OrderStatus.PAYMENT_PENDING
    )

    detail = client.get(f"/api/admin/orders/{pending.id}", headers=auth_headers).get_json()[
        "data"
    ]["order"]

    assert "PAID" not in detail["allowed_transitions"]
    assert detail["allowed_transitions"] == ["CANCELLED", "PAYMENT_FAILED"]


def test_a_delivered_order_offers_no_transitions(client, auth_headers, stocked_variant):
    delivered = make_order(stocked_variant, number="KC-D", status=OrderStatus.DELIVERED)

    detail = client.get(f"/api/admin/orders/{delivered.id}", headers=auth_headers).get_json()[
        "data"
    ]["order"]

    assert detail["allowed_transitions"] == []


def test_payment_details_exclude_the_raw_callback(client, auth_headers, order):
    """Business rule 8 — an admin sees the outcome, not the wire traffic."""
    db.session.add(
        Payment(
            order_id=order.id,
            status=PaymentStatus.SUCCESSFUL,
            amount=order.total,
            phone_number="254712000001",
            mpesa_receipt_number="RGX1A2B3C4",
            raw_callback='{"secret": "do-not-leak"}',
        )
    )
    db.session.commit()

    payment = client.get(f"/api/admin/orders/{order.id}", headers=auth_headers).get_json()[
        "data"
    ]["order"]["payments"][0]

    assert payment["mpesa_receipt_number"] == "RGX1A2B3C4"
    assert "raw_callback" not in payment
    assert "do-not-leak" not in str(payment)


# --- status transitions ----------------------------------------------------


def test_a_legal_transition_is_applied(client, auth_headers, order):
    response = client.patch(
        f"/api/admin/orders/{order.id}/status",
        headers=auth_headers,
        json={"status": "SHIPPED"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["order"]["status"] == "SHIPPED"


def test_a_delivered_order_cannot_go_back(client, auth_headers, stocked_variant):
    """Business rule 7."""
    delivered = make_order(stocked_variant, number="KC-D", status=OrderStatus.DELIVERED)

    response = client.patch(
        f"/api/admin/orders/{delivered.id}/status",
        headers=auth_headers,
        json={"status": "PAYMENT_PENDING"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "INVALID_STATUS_TRANSITION"
    assert db.session.get(Order, delivered.id).status == OrderStatus.DELIVERED


def test_an_admin_cannot_mark_an_order_paid(client, auth_headers, stocked_variant):
    """Business rule 3 — only a verified M-Pesa callback does that."""
    pending = make_order(
        stocked_variant, number="KC-P", status=OrderStatus.PAYMENT_PENDING
    )

    response = client.patch(
        f"/api/admin/orders/{pending.id}/status",
        headers=auth_headers,
        json={"status": "PAID"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PAYMENT_NOT_VERIFIED"


def test_an_unknown_status_is_rejected(client, auth_headers, order):
    response = client.patch(
        f"/api/admin/orders/{order.id}/status",
        headers=auth_headers,
        json={"status": "TELEPORTED"},
    )

    assert response.status_code == 422


def test_unknown_fields_are_rejected(client, auth_headers, order):
    response = client.patch(
        f"/api/admin/orders/{order.id}/status",
        headers=auth_headers,
        json={"status": "SHIPPED", "total": "1.00"},
    )

    assert response.status_code == 422


def test_setting_the_same_status_is_a_no_op(client, auth_headers, order):
    response = client.patch(
        f"/api/admin/orders/{order.id}/status",
        headers=auth_headers,
        json={"status": "PROCESSING"},
    )

    assert response.status_code == 200
    # Ignore the fixture's own sign-in row (sign-in is audited since hardening).
    assert db.session.scalar(
        select(AuditLog).where(AuditLog.entity_type != "admin_session")
    ) is None


# --- cancellation releases stock -------------------------------------------


def test_cancelling_returns_stock_through_the_ledger(client, auth_headers, stocked_variant):
    """Business rule 6."""
    pending = make_order(
        stocked_variant, number="KC-C", status=OrderStatus.PAYMENT_PENDING, quantity=2
    )
    # Simulate the reservation checkout will perform.
    stocked_variant.stock_quantity = 3
    db.session.commit()

    response = client.patch(
        f"/api/admin/orders/{pending.id}/status",
        headers=auth_headers,
        json={"status": "CANCELLED"},
    )

    assert response.status_code == 200
    assert stocked_variant.stock_quantity == 5

    movement = db.session.scalar(
        select(InventoryMovement).where(
            InventoryMovement.product_variant_id == stocked_variant.id
        )
    )
    assert movement.delta == 2
    assert movement.reason.value == "cancellation"
    assert movement.order_id == pending.id
    assert movement.admin_id is not None


def test_cancelling_stamps_the_time(client, auth_headers, stocked_variant):
    pending = make_order(stocked_variant, number="KC-C", status=OrderStatus.PAYMENT_PENDING)

    client.patch(
        f"/api/admin/orders/{pending.id}/status",
        headers=auth_headers,
        json={"status": "CANCELLED"},
    )

    assert db.session.get(Order, pending.id).cancelled_at is not None


def test_a_shipped_order_cannot_be_cancelled(client, auth_headers, stocked_variant):
    shipped = make_order(stocked_variant, number="KC-S", status=OrderStatus.SHIPPED)

    response = client.patch(
        f"/api/admin/orders/{shipped.id}/status",
        headers=auth_headers,
        json={"status": "CANCELLED"},
    )

    assert response.status_code == 409
    # Stock was not returned for a refused transition.
    assert stocked_variant.stock_quantity == 5


# --- audit -----------------------------------------------------------------


def test_a_status_change_is_audited(client, auth_headers, admin, order):
    client.patch(
        f"/api/admin/orders/{order.id}/status",
        headers=auth_headers,
        json={"status": "SHIPPED"},
    )

    entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "order.status"))
    assert entry.admin_email == admin.email
    assert "PROCESSING" in entry.previous_value
    assert "SHIPPED" in entry.new_value
