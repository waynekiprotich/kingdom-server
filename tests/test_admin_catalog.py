"""Admin catalog CRUD (spec §6, §13, §19)."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models.admin import Admin, AdminRole
from app.models.audit import AuditLog
from app.models.catalog import Category, Product, ProductStatus, ProductVariant
from app.models.inventory import InventoryMovement
from app.models.order import Order, OrderItem, OrderStatus
from tests.conftest import TEST_PASSWORD


@pytest.fixture
def staff_headers(client, app):
    """A non-superadmin admin, for checking role limits."""
    account = Admin(email="staff@example.com", name="Staff", role=AdminRole.STAFF)
    account.set_password(TEST_PASSWORD)
    db.session.add(account)
    db.session.commit()

    response = client.post(
        "/api/admin/auth/login",
        json={"email": "staff@example.com", "password": TEST_PASSWORD},
    )
    return {"Authorization": f"Bearer {response.get_json()['data']['access_token']}"}


@pytest.fixture
def category(app):
    record = Category(name="Shirts", slug="shirts", position=1)
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture
def product(app, category):
    record = Product(
        name="Linen Overshirt",
        slug="linen-overshirt",
        description="Mid-weight linen.",
        category_id=category.id,
        base_price=Decimal("4500.00"),
        status=ProductStatus.ACTIVE,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture
def variant(app, product):
    record = ProductVariant(
        product_id=product.id,
        sku="LINEN-M-BON",
        size="M",
        color="Bone",
        price=Decimal("4500.00"),
        stock_quantity=5,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture
def ordered_variant(app, variant):
    """A variant that appears on an order, so history must be preserved."""
    order = Order(
        order_number="KC-000123",
        status=OrderStatus.PAID,
        customer_name="Test",
        customer_phone="254712345678",
        delivery_location="Nairobi",
        subtotal=Decimal("4500.00"),
        delivery_fee=Decimal("0.00"),
        total=Decimal("4500.00"),
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_variant_id=variant.id,
            product_name="Linen Overshirt",
            variant_sku=variant.sku,
            size="M",
            color="Bone",
            unit_price=Decimal("4500.00"),
            quantity=1,
            line_total=Decimal("4500.00"),
        )
    )
    db.session.commit()
    return variant


# --- access control --------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/admin/products"),
        ("post", "/api/admin/products"),
        ("get", "/api/admin/categories"),
        ("post", "/api/admin/categories"),
        ("patch", "/api/admin/products/1"),
        ("delete", "/api/admin/products/1"),
        ("patch", "/api/admin/variants/1"),
        ("delete", "/api/admin/images/1"),
    ],
)
def test_every_admin_endpoint_requires_a_token(client, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 401


def test_a_deactivated_admin_is_refused_despite_a_valid_token(
    client, admin, auth_headers
):
    admin.is_active = False
    db.session.commit()

    response = client.get("/api/admin/products", headers=auth_headers)

    assert response.status_code == 403


def test_staff_cannot_delete_a_category(client, staff_headers, category):
    response = client.delete(
        f"/api/admin/categories/{category.id}", headers=staff_headers
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_superadmin_can_delete_a_category(client, auth_headers, category):
    assert (
        client.delete(
            f"/api/admin/categories/{category.id}", headers=auth_headers
        ).status_code
        == 200
    )


def test_staff_may_still_create_products(client, staff_headers, category):
    response = client.post(
        "/api/admin/products",
        headers=staff_headers,
        json={"name": "Staff Product", "category_id": category.id, "base_price": "100.00"},
    )

    assert response.status_code == 201


# --- categories ------------------------------------------------------------


def test_create_category_generates_a_slug(client, auth_headers):
    response = client.post(
        "/api/admin/categories", headers=auth_headers, json={"name": "Outer Wear"}
    )

    assert response.status_code == 201
    assert response.get_json()["data"]["category"]["slug"] == "outer-wear"


def test_create_category_folds_accents_into_the_slug(client, auth_headers):
    response = client.post(
        "/api/admin/categories", headers=auth_headers, json={"name": "Écru Basics"}
    )

    assert response.get_json()["data"]["category"]["slug"] == "ecru-basics"


def test_a_duplicate_category_slug_is_a_conflict(client, auth_headers, category):
    response = client.post(
        "/api/admin/categories",
        headers=auth_headers,
        json={"name": "Another", "slug": "shirts"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "SLUG_TAKEN"


def test_category_listing_counts_products_including_drafts(
    client, auth_headers, category, product
):
    product.status = ProductStatus.DRAFT
    db.session.commit()

    categories = client.get("/api/admin/categories", headers=auth_headers).get_json()[
        "data"
    ]["categories"]

    assert categories[0]["product_count"] == 1


def test_update_category_reports_what_changed(client, auth_headers, category):
    response = client.patch(
        f"/api/admin/categories/{category.id}",
        headers=auth_headers,
        json={"name": "Shirting", "position": 4},
    )

    assert response.status_code == 200
    assert set(response.get_json()["data"]["changed"]) == {"name", "position"}


def test_a_category_with_products_cannot_be_deleted(
    client, auth_headers, category, product
):
    response = client.delete(f"/api/admin/categories/{category.id}", headers=auth_headers)

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "CATEGORY_NOT_EMPTY"
    assert db.session.get(Category, category.id) is not None


def test_unknown_category_is_a_404(client, auth_headers):
    response = client.get("/api/admin/categories/99999", headers=auth_headers)

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "CATEGORY_NOT_FOUND"


# --- products --------------------------------------------------------------


def test_a_new_product_starts_as_a_draft(client, auth_headers, category):
    response = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={"name": "New Shirt", "category_id": category.id, "base_price": "3200.00"},
    )

    assert response.status_code == 201
    created = response.get_json()["data"]["product"]
    assert created["status"] == "draft"
    assert created["slug"] == "new-shirt"

    # A draft must not appear in the public catalog.
    assert client.get("/api/products/new-shirt").status_code == 404


def test_generated_slugs_avoid_collisions(client, auth_headers, category, product):
    response = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={
            "name": "Linen Overshirt",
            "category_id": category.id,
            "base_price": "1.00",
        },
    )

    assert response.get_json()["data"]["product"]["slug"] == "linen-overshirt-2"


def test_an_explicit_duplicate_slug_is_a_conflict(client, auth_headers, category, product):
    """A slug the admin typed is never silently altered."""
    response = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={
            "name": "Other",
            "slug": "linen-overshirt",
            "category_id": category.id,
            "base_price": "1.00",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "SLUG_TAKEN"


def test_a_product_needs_a_real_category(client, auth_headers):
    response = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={"name": "Orphan", "category_id": 4242, "base_price": "10.00"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "CATEGORY_NOT_FOUND"


def test_a_sale_price_must_be_below_the_base_price(client, auth_headers, category):
    response = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={
            "name": "Bad Sale",
            "category_id": category.id,
            "base_price": "1000.00",
            "sale_price": "1000.00",
        },
    )

    assert response.status_code == 422


def test_a_patched_sale_price_is_checked_against_the_stored_base_price(
    client, auth_headers, product
):
    """Only sale_price is sent; validation still has to see the real base."""
    response = client.patch(
        f"/api/admin/products/{product.id}",
        headers=auth_headers,
        json={"sale_price": "9999.00"},
    )

    assert response.status_code == 422


def test_admin_listing_shows_drafts_and_archived(client, auth_headers, category, product):
    product.status = ProductStatus.ARCHIVED
    db.session.commit()

    listed = client.get("/api/admin/products", headers=auth_headers).get_json()["data"]

    assert [p["slug"] for p in listed["products"]] == ["linen-overshirt"]
    assert listed["products"][0]["status"] == "archived"
    # ...and stays hidden from customers.
    assert client.get("/api/products").get_json()["data"]["products"] == []


def test_admin_listing_filters_by_status(client, auth_headers, category, product):
    client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={"name": "Draft One", "category_id": category.id, "base_price": "1.00"},
    )

    drafts = client.get("/api/admin/products?status=draft", headers=auth_headers).get_json()[
        "data"
    ]["products"]

    assert [p["name"] for p in drafts] == ["Draft One"]


def test_admin_listing_rejects_an_unknown_status(client, auth_headers):
    response = client.get("/api/admin/products?status=nonsense", headers=auth_headers)

    assert response.status_code == 422


def test_admin_listing_searches_name_and_slug(client, auth_headers, product):
    found = client.get("/api/admin/products?q=overshirt", headers=auth_headers).get_json()[
        "data"
    ]["products"]

    assert len(found) == 1


def test_admin_summary_reports_stock_totals(client, auth_headers, product, variant):
    listed = client.get("/api/admin/products", headers=auth_headers).get_json()["data"][
        "products"
    ][0]

    assert listed["variant_count"] == 1
    assert listed["total_stock"] == 5


def test_delete_archives_rather_than_destroying(client, auth_headers, product):
    response = client.delete(f"/api/admin/products/{product.id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.get_json()["data"]["product"]["status"] == "archived"
    assert db.session.get(Product, product.id) is not None


def test_permanent_delete_needs_a_superadmin(client, staff_headers, product):
    response = client.delete(
        f"/api/admin/products/{product.id}?permanent=true", headers=staff_headers
    )

    assert response.status_code == 409
    assert db.session.get(Product, product.id) is not None


def test_a_superadmin_can_permanently_delete_an_unsold_product(
    client, auth_headers, product
):
    response = client.delete(
        f"/api/admin/products/{product.id}?permanent=true", headers=auth_headers
    )

    assert response.status_code == 200
    assert db.session.get(Product, product.id) is None


def test_a_sold_product_cannot_be_permanently_deleted(
    client, auth_headers, product, ordered_variant
):
    """Order history has to survive the catalog."""
    response = client.delete(
        f"/api/admin/products/{product.id}?permanent=true", headers=auth_headers
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "PRODUCT_HAS_ORDERS"
    assert db.session.get(Product, product.id) is not None


# --- variants --------------------------------------------------------------


def test_creating_a_variant_records_its_opening_stock_as_a_movement(
    client, auth_headers, product
):
    response = client.post(
        f"/api/admin/products/{product.id}/variants",
        headers=auth_headers,
        json={"size": "L", "color": "Olive", "price": "4500.00", "stock_quantity": 7},
    )

    assert response.status_code == 201
    created = response.get_json()["data"]["variant"]
    assert created["stock_quantity"] == 7

    movements = db.session.scalars(
        select(InventoryMovement).where(
            InventoryMovement.product_variant_id == created["id"]
        )
    ).all()
    assert [m.delta for m in movements] == [7]
    assert movements[0].admin_id is not None


def test_a_generated_sku_has_no_doubled_separators(client, auth_headers, category):
    """Truncating a slug can land on a hyphen; the SKU must not show it."""
    product_id = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={"name": "Merino Crew Neck", "category_id": category.id, "base_price": "1.00"},
    ).get_json()["data"]["product"]["id"]

    sku = client.post(
        f"/api/admin/products/{product_id}/variants",
        headers=auth_headers,
        json={"size": "M", "color": "Charcoal", "price": "1.00"},
    ).get_json()["data"]["variant"]["sku"]

    assert "--" not in sku
    assert sku == "MERINO-CREW-M-CHA"


def test_a_duplicate_size_and_colour_is_a_conflict(client, auth_headers, product, variant):
    response = client.post(
        f"/api/admin/products/{product.id}/variants",
        headers=auth_headers,
        json={"size": "M", "color": "Bone", "price": "1.00"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "VARIANT_EXISTS"


def test_a_duplicate_sku_is_a_conflict(client, auth_headers, product, variant):
    response = client.post(
        f"/api/admin/products/{product.id}/variants",
        headers=auth_headers,
        json={"size": "L", "color": "Navy", "price": "1.00", "sku": "LINEN-M-BON"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "SKU_TAKEN"


def test_adjusting_stock_writes_a_signed_movement(client, auth_headers, variant):
    client.patch(
        f"/api/admin/variants/{variant.id}",
        headers=auth_headers,
        json={"stock_quantity": 2},
    )

    movement = db.session.scalar(
        select(InventoryMovement).where(
            InventoryMovement.product_variant_id == variant.id
        )
    )
    assert movement.delta == -3  # 5 -> 2
    assert movement.reason.value == "adjustment"
    assert variant.stock_quantity == 2


def test_an_unchanged_stock_value_writes_no_movement(client, auth_headers, variant):
    client.patch(
        f"/api/admin/variants/{variant.id}",
        headers=auth_headers,
        json={"stock_quantity": 5, "price": "4600.00"},
    )

    assert (
        db.session.scalar(
            select(InventoryMovement).where(
                InventoryMovement.product_variant_id == variant.id
            )
        )
        is None
    )


def test_stock_cannot_be_set_negative(client, auth_headers, variant):
    response = client.patch(
        f"/api/admin/variants/{variant.id}",
        headers=auth_headers,
        json={"stock_quantity": -1},
    )

    assert response.status_code == 422


def test_an_unsold_variant_is_deleted(client, auth_headers, variant):
    response = client.delete(f"/api/admin/variants/{variant.id}", headers=auth_headers)

    assert response.status_code == 200
    assert db.session.get(ProductVariant, variant.id) is None


def test_a_sold_variant_is_deactivated_not_deleted(client, auth_headers, ordered_variant):
    response = client.delete(
        f"/api/admin/variants/{ordered_variant.id}", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["deactivated"] is True
    assert db.session.get(ProductVariant, ordered_variant.id).status.value == "inactive"


# --- images ----------------------------------------------------------------


def test_an_image_requires_alt_text(client, auth_headers, product):
    response = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "products/shirt"},
    )

    assert response.status_code == 422
    assert "alt_text" in response.get_json()["error"]["message"]


def test_an_image_needs_a_source(client, auth_headers, product):
    response = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"alt_text": "A shirt"},
    )

    assert response.status_code == 422


def test_a_blank_public_id_is_not_a_source(client, auth_headers, product):
    """Whitespace is not a photograph.

    The database only checks that one of public_id/url is non-NULL, so an
    empty string would slip past it and leave a row that renders as a broken
    image with no way to tell why.
    """
    response = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "   ", "alt_text": "A shirt"},
    )

    assert response.status_code == 422


def test_an_image_cannot_be_blanked_by_update(client, auth_headers, product):
    created = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "products/shirt", "alt_text": "A shirt"},
    )
    image_id = created.get_json()["data"]["image"]["id"]

    response = client.patch(
        f"/api/admin/images/{image_id}",
        headers=auth_headers,
        json={"public_id": "  "},
    )

    assert response.status_code == 422


def test_the_first_image_becomes_the_primary_one(client, auth_headers, product):
    response = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "products/one", "alt_text": "Front view"},
    )

    assert response.get_json()["data"]["image"]["is_primary"] is True
    assert response.get_json()["data"]["image"]["position"] == 0


def test_only_one_image_can_be_primary(client, auth_headers, product):
    first = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "products/one", "alt_text": "Front"},
    ).get_json()["data"]["image"]

    second = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "products/two", "alt_text": "Back", "is_primary": True},
    ).get_json()["data"]["image"]

    images = client.get(
        f"/api/admin/products/{product.id}", headers=auth_headers
    ).get_json()["data"]["product"]["images"]
    primaries = [image["id"] for image in images if image["is_primary"]]

    assert primaries == [second["id"]]
    assert first["id"] not in primaries


def test_deleting_the_primary_image_promotes_another(client, auth_headers, product):
    first = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "products/one", "alt_text": "Front"},
    ).get_json()["data"]["image"]
    client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth_headers,
        json={"public_id": "products/two", "alt_text": "Back"},
    )

    client.delete(f"/api/admin/images/{first['id']}", headers=auth_headers)

    images = client.get(
        f"/api/admin/products/{product.id}", headers=auth_headers
    ).get_json()["data"]["product"]["images"]

    assert len(images) == 1
    assert images[0]["is_primary"] is True


# --- validation ------------------------------------------------------------


def test_unknown_fields_are_rejected(client, auth_headers, category):
    """A typo must fail loudly, not be silently dropped."""
    response = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={
            "name": "Typo",
            "category_id": category.id,
            "base_price": "1.00",
            "stock_quantiy": 5,
        },
    )

    assert response.status_code == 422
    assert "stock_quantiy" in response.get_json()["error"]["message"]


def test_status_cannot_be_set_by_mass_assignment_of_an_unknown_field(
    client, auth_headers, product
):
    response = client.patch(
        f"/api/admin/products/{product.id}",
        headers=auth_headers,
        json={"created_at": "2020-01-01T00:00:00Z"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"name": 42},
        {"name": "Ok", "base_price": "free"},
        {"name": "Ok", "base_price": -1},
        {"name": "Ok", "base_price": True},
        {"name": "Ok", "base_price": "10.999"},
        {"name": "Ok", "base_price": "1.00", "featured": "yes"},
        {"name": "Ok", "base_price": "1.00", "status": "published"},
    ],
)
def test_malformed_product_payloads_are_rejected(client, auth_headers, category, payload):
    response = client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={"category_id": category.id, **payload},
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_explicit_null_clears_a_nullable_field(client, auth_headers, product):
    product.sale_price = Decimal("4000.00")
    db.session.commit()

    response = client.patch(
        f"/api/admin/products/{product.id}",
        headers=auth_headers,
        json={"sale_price": None},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["product"]["sale_price"] is None


def test_an_absent_field_is_left_alone(client, auth_headers, product):
    """PATCH must distinguish "not sent" from "set to null"."""
    original = product.description

    client.patch(
        f"/api/admin/products/{product.id}",
        headers=auth_headers,
        json={"name": "Renamed"},
    )

    assert product.description == original


# --- audit trail -----------------------------------------------------------


def test_creating_a_product_writes_an_audit_row(client, auth_headers, admin, category):
    client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={"name": "Audited", "category_id": category.id, "base_price": "1.00"},
    )

    entry = db.session.scalar(
        select(AuditLog).where(AuditLog.action == "product.create")
    )
    assert entry.admin_email == admin.email
    assert entry.entity_type == "product"
    assert "Audited" in entry.new_value


def test_an_update_records_the_previous_and_new_values(client, auth_headers, product):
    client.patch(
        f"/api/admin/products/{product.id}",
        headers=auth_headers,
        json={"name": "Renamed Overshirt"},
    )

    entry = db.session.scalar(
        select(AuditLog).where(AuditLog.action == "product.update")
    )
    assert "Linen Overshirt" in entry.previous_value
    assert "Renamed Overshirt" in entry.new_value


def test_a_no_op_update_writes_no_audit_row(client, auth_headers, product):
    client.patch(
        f"/api/admin/products/{product.id}",
        headers=auth_headers,
        json={"name": product.name},
    )

    assert (
        db.session.scalar(select(AuditLog).where(AuditLog.action == "product.update"))
        is None
    )


def test_a_rejected_request_writes_no_audit_noise(client, auth_headers, category, product):
    """A request refused during validation records nothing."""
    client.post(
        "/api/admin/products",
        headers=auth_headers,
        json={
            "name": "Rejected",
            "slug": "linen-overshirt",
            "category_id": category.id,
            "base_price": "1.00",
        },
    )

    # Sign-in is audited too since the security hardening; the fixture's own
    # login row is not noise from this request.
    assert db.session.scalar(
        select(AuditLog).where(AuditLog.entity_type != "admin_session")
    ) is None


def test_a_constraint_violation_becomes_a_409_and_rolls_back(app, client, category):
    """The backstop for races the proactive checks cannot win.

    Two requests can both pass a uniqueness check and then collide at commit.
    That must be a conflict, not a 500, and must leave nothing behind.
    """
    from app.models.catalog import Category as CategoryModel

    @app.post("/__test__/duplicate")
    def duplicate():  # pragma: no cover - registered per test
        db.session.add(CategoryModel(name="Clash", slug=category.slug))
        db.session.commit()
        return {"success": True}

    response = client.post("/__test__/duplicate")

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "CONFLICT"
    # The failed transaction was rolled back, so the session is usable again.
    assert db.session.scalar(select(func.count(CategoryModel.id))) == 1
