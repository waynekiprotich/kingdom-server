from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app import create_app
from app.extensions import db as _db
from app.models.admin import Admin, AdminRole
from app.models.catalog import Category, Product, ProductStatus, ProductVariant
from app.models.order import Order, OrderStatus

TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        if _db.engine.dialect.name == "sqlite":
            # SQLite ignores foreign keys unless asked. Turn them on so
            # ondelete rules behave the way they will on PostgreSQL.
            _db.session.execute(text("PRAGMA foreign_keys=ON"))
        _db.drop_all()
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()
        # Every test builds its own app, and so its own engine and pool. The
        # pools linger until garbage collection, which is enough to exhaust
        # PostgreSQL's connection slots part-way through a full run now that
        # rate limiting checks out a connection of its own.
        _db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    account = Admin(email="owner@example.com", name="Owner", role=AdminRole.SUPERADMIN)
    account.set_password(TEST_PASSWORD)
    _db.session.add(account)
    _db.session.commit()
    return account


@pytest.fixture
def tokens(client, admin):
    response = client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    return response.get_json()["data"]


@pytest.fixture
def auth_headers(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def variant(app):
    category = Category(name="Shirts", slug="shirts")
    _db.session.add(category)
    _db.session.flush()

    product = Product(
        name="Test Product",
        slug="test-product",
        category_id=category.id,
        base_price=Decimal("2500.00"),
        status=ProductStatus.ACTIVE,
    )
    _db.session.add(product)
    _db.session.flush()

    item = ProductVariant(
        product_id=product.id,
        sku="TEST-M-BLACK",
        size="M",
        color="Black",
        price=Decimal("2500.00"),
        stock_quantity=5,
    )
    _db.session.add(item)
    _db.session.commit()
    return item


@pytest.fixture
def order(app):
    record = Order(
        order_number="KC-000001",
        status=OrderStatus.PAYMENT_PENDING,
        customer_name="Test Customer",
        customer_phone="254712345678",
        delivery_location="Nairobi CBD",
        subtotal=Decimal("2500.00"),
        delivery_fee=Decimal("300.00"),
        total=Decimal("2800.00"),
    )
    _db.session.add(record)
    _db.session.commit()
    return record
