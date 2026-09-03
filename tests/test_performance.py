"""Transfer-level performance: compression and public cache validity.

These guard properties that are invisible until they regress — nothing in the
UI breaks when compression silently stops happening, the shop just gets
slower on the connections it is actually browsed over.
"""

from __future__ import annotations

import gzip
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.catalog import Category, Product, ProductStatus
from app.performance import MIN_COMPRESS_BYTES


def _add_product(name: str, category: Category) -> Product:
    product = Product(
        name=name,
        slug=name.lower().replace(" ", "-"),
        description="A long enough description that a page of these is worth "
        "compressing, which is the condition under test.",
        category_id=category.id,
        base_price=Decimal("4200.00"),
        status=ProductStatus.ACTIVE,
    )
    db.session.add(product)
    return product


@pytest.fixture
def catalog(app):
    """Enough products that the response clears the compression threshold."""
    category = Category(name="Trousers", slug="trousers")
    db.session.add(category)
    db.session.flush()

    for index in range(12):
        _add_product(f"Piece {index}", category)
    db.session.commit()

    return category


def test_a_catalog_page_is_compressed_when_the_client_accepts_it(client, catalog):
    response = client.get("/api/products", headers={"Accept-Encoding": "gzip"})

    assert response.headers["Content-Encoding"] == "gzip"
    # The body must actually be gzip, not merely labelled as such.
    assert gzip.decompress(response.get_data())


def test_compression_shrinks_the_payload(client, catalog):
    compressed = client.get("/api/products", headers={"Accept-Encoding": "gzip"})
    plain = client.get("/api/products", headers={"Accept-Encoding": "identity"})

    assert len(compressed.get_data()) < len(plain.get_data())


def test_a_client_that_cannot_decompress_gets_plain_json(client, catalog):
    """The one way this optimisation could break a customer's browser."""
    response = client.get("/api/products", headers={"Accept-Encoding": "identity"})

    assert "Content-Encoding" not in response.headers
    assert response.get_json()["success"] is True


def test_compressed_responses_vary_on_accept_encoding(client, catalog):
    """Without this a shared cache can hand a gzipped body to a client that
    never asked for one."""
    response = client.get("/api/products", headers={"Accept-Encoding": "gzip"})

    assert "Accept-Encoding" in response.headers["Vary"]


def test_small_responses_are_left_alone(client):
    """Below a packet's worth, gzip's own header costs more than it saves."""
    response = client.get("/health", headers={"Accept-Encoding": "gzip"})

    assert len(response.get_data()) < MIN_COMPRESS_BYTES
    assert "Content-Encoding" not in response.headers


# --- cache validity ---------------------------------------------------------


def test_the_catalog_is_publicly_cacheable(client):
    cache_control = client.get("/api/products").headers["Cache-Control"]

    assert "public" in cache_control
    assert "max-age=" in cache_control


def test_an_unchanged_catalog_revalidates_to_304(client, catalog):
    """The point of the ETag: a repeat visit costs a round trip, not a payload."""
    etag = client.get("/api/products").headers["ETag"]

    response = client.get("/api/products", headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.get_data() == b""


def test_an_edited_catalog_gets_a_new_etag(client, catalog):
    """A stale ETag would serve a customer yesterday's shop."""
    before = client.get("/api/products").headers["ETag"]

    _add_product("Just added", catalog)
    db.session.commit()

    assert client.get("/api/products").headers["ETag"] != before


def test_personal_data_is_never_publicly_cached(client, auth_headers):
    """The rule this optimisation must not weaken: an admin's view of the
    business, and a guest's own order, stay out of shared caches."""
    cache_control = client.get("/api/admin/orders", headers=auth_headers).headers[
        "Cache-Control"
    ]

    assert cache_control == "no-store"
    assert "public" not in cache_control
