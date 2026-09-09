"""Guards on how many queries a listing costs, not just what it returns.

An N+1 is invisible from the outside: the JSON is correct, the tests pass, and
the page simply gets slower every time the catalog grows. The admin product
listing had one — 63 queries to render 25 rows — and nothing failed. These
tests assert the property that was actually broken: **a listing's query count
must not depend on how many rows it returns.**
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.blueprints import catalog as catalog_blueprint
from app.extensions import db
from app.models.catalog import (
    Category,
    Product,
    ProductImage,
    ProductStatus,
    ProductVariant,
)
from app.models.order import Order, OrderItem, OrderStatus


@pytest.fixture
def count_queries(app):
    """Count the statements a block of work sends to the database."""

    @contextmanager
    def counter():
        seen: list[str] = []

        def record(conn, cursor, statement, params, context, executemany):
            seen.append(statement)

        event.listen(db.engine, "before_cursor_execute", record)
        try:
            yield seen
        finally:
            event.remove(db.engine, "before_cursor_execute", record)

    return counter


def _catalog(product_count: int) -> Category:
    """A category of products that each have a variant and an image.

    Those two relationships plus the category are exactly what the summary
    serializers read, so they are what an N+1 would show up in.
    """
    category = Category(name="Shirts", slug="shirts")
    db.session.add(category)
    db.session.flush()

    for index in range(product_count):
        product = Product(
            name=f"Piece {index}",
            slug=f"piece-{index}",
            description="Cotton, cut in Nairobi.",
            category_id=category.id,
            base_price=Decimal("3500.00"),
            status=ProductStatus.ACTIVE,
        )
        db.session.add(product)
        db.session.flush()

        db.session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"PIECE-{index}-M",
                size="M",
                color="Black",
                price=Decimal("3500.00"),
                stock_quantity=4,
            )
        )
        db.session.add(
            ProductImage(
                product_id=product.id,
                public_id=f"products/piece-{index}",
                alt_text=f"Piece {index}",
                is_primary=True,
            )
        )

    db.session.commit()
    return category


# --- the property that broke ------------------------------------------------


def test_admin_product_listing_does_not_grow_queries_with_rows(
    client, auth_headers, count_queries
):
    """The N+1 regression guard.

    Before the fix this was 3 extra queries per product — variants, images and
    category, lazy-loaded one row at a time.
    """
    _catalog(2)
    with count_queries() as small:
        assert client.get("/api/admin/products", headers=auth_headers).status_code == 200

    _catalog_more = _catalog_extra(8)
    with count_queries() as large:
        assert client.get("/api/admin/products", headers=auth_headers).status_code == 200

    assert len(large) == len(small), (
        f"Admin product listing cost {len(small)} queries for 2 products and "
        f"{len(large)} for 10 — it is lazy-loading per row again. Add the "
        f"relationship to _summary_loaders()."
    )


def test_public_product_listing_does_not_grow_queries_with_rows(client, count_queries):
    _catalog(2)
    with count_queries() as small:
        assert client.get("/api/products").status_code == 200

    _catalog_extra(8)
    with count_queries() as large:
        assert client.get("/api/products").status_code == 200

    assert len(large) == len(small)


def _catalog_extra(product_count: int) -> None:
    """Add more products to the category the first fixture made."""
    category = db.session.scalar(db.select(Category).where(Category.slug == "shirts"))
    for index in range(100, 100 + product_count):
        product = Product(
            name=f"Piece {index}",
            slug=f"piece-{index}",
            description="Cotton, cut in Nairobi.",
            category_id=category.id,
            base_price=Decimal("3500.00"),
            status=ProductStatus.ACTIVE,
        )
        db.session.add(product)
        db.session.flush()
        db.session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"PIECE-{index}-M",
                size="M",
                color="Black",
                price=Decimal("3500.00"),
                stock_quantity=4,
            )
        )
        db.session.add(
            ProductImage(
                product_id=product.id,
                public_id=f"products/piece-{index}",
                alt_text=f"Piece {index}",
                is_primary=True,
            )
        )
    db.session.commit()


# --- the correlated order count still counts correctly ----------------------


def test_admin_order_listing_reports_the_right_line_count(
    client, auth_headers, variant
):
    """The count moved from a grouped join to a correlated subquery; the
    numbers it produces must not have moved with it."""
    order = Order(
        order_number="KC-000900",
        status=OrderStatus.PAID,
        customer_name="Wanjiku",
        customer_phone="254712345678",
        delivery_location="Kilimani",
        subtotal=Decimal("7000.00"),
        delivery_fee=Decimal("300.00"),
        total=Decimal("7300.00"),
    )
    db.session.add(order)
    db.session.flush()

    for index in range(3):
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_variant_id=variant.id,
                product_name=f"Piece {index}",
                variant_sku=f"SKU-{index}",
                size="M",
                color="Black",
                unit_price=Decimal("2000.00"),
                quantity=1,
                line_total=Decimal("2000.00"),
            )
        )
    db.session.commit()

    body = client.get("/api/admin/orders", headers=auth_headers).get_json()
    listed = next(o for o in body["data"]["orders"] if o["order_number"] == "KC-000900")

    assert listed["item_count"] == 3


def test_an_order_with_no_lines_counts_zero(client, auth_headers, order):
    """The old outer join coalesced a missing row to 0. A correlated COUNT
    returns 0 on its own — but only if it really is correlated."""
    body = client.get("/api/admin/orders", headers=auth_headers).get_json()
    listed = next(
        o for o in body["data"]["orders"] if o["order_number"] == order.order_number
    )

    assert listed["item_count"] == 0


# --- the facet cache --------------------------------------------------------


@pytest.fixture
def facet_cache_on(app):
    """Turn the cache on for one test and leave nothing behind.

    It is off in testing precisely because it is module-level state that
    outlives the per-test database, so a test that wants it must also clean it.
    """
    catalog_blueprint._facet_cache.clear()
    app.config["FACET_CACHE_SECONDS"] = 60
    yield
    catalog_blueprint._facet_cache.clear()
    app.config["FACET_CACHE_SECONDS"] = 0


def test_facets_are_not_recomputed_within_the_cache_window(
    client, count_queries, facet_cache_on
):
    """Two queries per request are the facet ones. On the second request
    inside the window they should not be sent at all."""
    _catalog(3)

    with count_queries() as first:
        client.get("/api/products")
    with count_queries() as second:
        client.get("/api/products")

    assert len(second) < len(first)


def test_a_cached_facet_set_is_still_the_right_answer(client, facet_cache_on):
    _catalog(3)

    first = client.get("/api/products").get_json()["data"]["filters"]
    second = client.get("/api/products").get_json()["data"]["filters"]

    assert second["sizes"] == first["sizes"] == ["M"]
    assert second["colors"] == first["colors"] == ["Black"]


def test_scopes_do_not_share_a_cache_entry(client, facet_cache_on):
    """A category's facets must never be served for the whole catalog, or for
    a different category. The scope key is what prevents it."""
    category = _catalog(2)

    other = Category(name="Coats", slug="coats")
    db.session.add(other)
    db.session.flush()
    coat = Product(
        name="Long Coat",
        slug="long-coat",
        description="Wool.",
        category_id=other.id,
        base_price=Decimal("9000.00"),
        status=ProductStatus.ACTIVE,
    )
    db.session.add(coat)
    db.session.flush()
    db.session.add(
        ProductVariant(
            product_id=coat.id,
            sku="COAT-XL-CAMEL",
            size="XL",
            color="Camel",
            price=Decimal("9000.00"),
            stock_quantity=2,
        )
    )
    db.session.commit()

    shirts = client.get(f"/api/products?category={category.slug}").get_json()
    coats = client.get("/api/products?category=coats").get_json()

    assert shirts["data"]["filters"]["colors"] == ["Black"]
    assert coats["data"]["filters"]["colors"] == ["Camel"]


def test_the_cache_is_bounded(client, facet_cache_on):
    """The scope key contains the caller's search term, so an unbounded cache
    would be a memory-exhaustion bug with a query string for a key."""
    _catalog(2)

    for index in range(catalog_blueprint.FACET_CACHE_MAX_ENTRIES + 40):
        client.get(f"/api/products?q=term-{index}")

    assert (
        len(catalog_blueprint._facet_cache)
        <= catalog_blueprint.FACET_CACHE_MAX_ENTRIES
    )
