"""Public catalog endpoints (spec §6, §13, §20)."""

from decimal import Decimal

import pytest

from app.extensions import db
from app.models.catalog import (
    Category,
    Product,
    ProductImage,
    ProductStatus,
    ProductVariant,
    VariantStatus,
)


def make_product(
    *,
    name,
    slug,
    category,
    price="1000.00",
    sale_price=None,
    status=ProductStatus.ACTIVE,
    featured=False,
    description="",
    variants=(("M", "Black", 5),),
    images=(),
):
    product = Product(
        name=name,
        slug=slug,
        description=description,
        category_id=category.id,
        base_price=Decimal(price),
        sale_price=Decimal(sale_price) if sale_price else None,
        status=status,
        featured=featured,
    )
    db.session.add(product)
    db.session.flush()

    for size, color, stock in variants:
        db.session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"{slug[:10].upper()}-{size}-{color[:3].upper()}",
                size=size,
                color=color,
                price=Decimal(sale_price or price),
                stock_quantity=stock,
            )
        )
    for position, public_id in enumerate(images):
        db.session.add(
            ProductImage(
                product_id=product.id,
                public_id=public_id,
                alt_text=f"{name} photograph",
                position=position,
                is_primary=position == 0,
            )
        )
    return product


@pytest.fixture
def catalog(app):
    """A deterministic catalog covering the cases the storefront must handle."""
    shirts = Category(name="Shirts", slug="shirts", position=1)
    bags = Category(name="Bags", slug="bags", position=2)
    empty = Category(name="Empty", slug="empty", position=3)
    db.session.add_all([shirts, bags, empty])
    db.session.flush()

    make_product(
        name="Linen Overshirt",
        slug="linen-overshirt",
        category=shirts,
        price="4500.00",
        sale_price="3600.00",
        featured=True,
        description="Mid-weight linen with a boxy shoulder.",
        variants=(("S", "Bone", 3), ("M", "Bone", 4), ("M", "Olive", 0)),
        images=("shirt-front", "shirt-back"),
    )
    make_product(
        name="Poplin Shirt",
        slug="poplin-shirt",
        category=shirts,
        price="3200.00",
        description="Crisp cotton poplin.",
        variants=(("M", "Black", 2),),
    )
    make_product(
        name="Sold Out Shirt",
        slug="sold-out-shirt",
        category=shirts,
        price="2800.00",
        variants=(("M", "Navy", 0),),
    )
    make_product(
        name="Market Tote",
        slug="market-tote",
        category=bags,
        price="7400.00",
        description="Single-piece leather.",
        variants=(("One size", "Tan", 6),),
    )
    make_product(
        name="Hidden Draft",
        slug="hidden-draft",
        category=shirts,
        price="999.00",
        status=ProductStatus.DRAFT,
    )
    db.session.commit()
    return {"shirts": shirts, "bags": bags, "empty": empty}


def products(response):
    return response.get_json()["data"]["products"]


def slugs(response):
    return [item["slug"] for item in products(response)]


# --- listing ---------------------------------------------------------------


def test_only_active_products_are_public(client, catalog):
    """A draft exists in the database and must never leave it."""
    assert "hidden-draft" not in slugs(client.get("/api/products"))
    assert client.get("/api/products/hidden-draft").status_code == 404


def test_listing_reports_stock_without_publishing_counts(client, catalog):
    listed = {item["slug"]: item for item in products(client.get("/api/products"))}

    assert listed["poplin-shirt"]["in_stock"] is True
    assert listed["sold-out-shirt"]["in_stock"] is False
    assert "stock_quantity" not in listed["poplin-shirt"]


def test_summary_does_not_leak_internal_fields(client, catalog):
    item = products(client.get("/api/products"))[0]

    for leaked in ("status", "category_id", "created_at", "updated_at", "stock_quantity"):
        assert leaked not in item


def test_sale_price_becomes_the_effective_price(client, catalog):
    listed = {item["slug"]: item for item in products(client.get("/api/products"))}
    overshirt = listed["linen-overshirt"]

    assert overshirt["price"] == "3600.00"
    assert overshirt["base_price"] == "4500.00"
    assert overshirt["on_sale"] is True
    assert listed["poplin-shirt"]["on_sale"] is False


# --- pagination ------------------------------------------------------------


def test_pagination_splits_results_and_reports_position(client, catalog):
    first = client.get("/api/products?per_page=2&page=1").get_json()["data"]
    second = client.get("/api/products?per_page=2&page=2").get_json()["data"]

    assert first["pagination"] == {
        "page": 1,
        "per_page": 2,
        "total": 4,
        "total_pages": 2,
        "has_prev": False,
        "has_next": True,
    }
    assert second["pagination"]["has_next"] is False
    assert second["pagination"]["has_prev"] is True

    # No product appears on two pages.
    assert not {p["slug"] for p in first["products"]} & {
        p["slug"] for p in second["products"]
    }


def test_a_page_beyond_the_end_is_empty_not_an_error(client, catalog):
    response = client.get("/api/products?page=99")

    assert response.status_code == 200
    assert products(response) == []


# --- category filtering ----------------------------------------------------


def test_category_filter_narrows_results(client, catalog):
    response = client.get("/api/products?category=shirts")

    assert set(slugs(response)) == {"linen-overshirt", "poplin-shirt", "sold-out-shirt"}
    assert response.get_json()["data"]["filters"]["category"]["name"] == "Shirts"


def test_unknown_category_is_a_404(client, catalog):
    response = client.get("/api/products?category=does-not-exist")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "CATEGORY_NOT_FOUND"


def test_an_empty_category_returns_no_products(client, catalog):
    response = client.get("/api/products?category=empty")

    assert response.status_code == 200
    assert products(response) == []


def test_categories_carry_active_product_counts(client, catalog):
    counts = {
        item["slug"]: item["product_count"]
        for item in client.get("/api/categories").get_json()["data"]["categories"]
    }

    assert counts == {"shirts": 3, "bags": 1, "empty": 0}  # draft not counted


# --- search ----------------------------------------------------------------


def test_search_matches_the_name(client, catalog):
    assert slugs(client.get("/api/products?q=poplin")) == ["poplin-shirt"]


def test_search_matches_the_description(client, catalog):
    assert slugs(client.get("/api/products?q=leather")) == ["market-tote"]


def test_search_is_case_insensitive(client, catalog):
    assert slugs(client.get("/api/products?q=LINEN")) == ["linen-overshirt"]


def test_search_with_no_matches_is_an_empty_list(client, catalog):
    response = client.get("/api/products?q=zzzznothing")

    assert response.status_code == 200
    assert products(response) == []
    assert response.get_json()["data"]["pagination"]["total"] == 0


# --- sorting ---------------------------------------------------------------


def test_sort_by_price_ascending_uses_the_effective_price(client, catalog):
    ordered = slugs(client.get("/api/products?sort=price_asc"))

    # Overshirt sorts by its 3600 sale price, not its 4500 base price.
    assert ordered == ["sold-out-shirt", "poplin-shirt", "linen-overshirt", "market-tote"]


def test_sort_by_price_descending_reverses_it(client, catalog):
    assert slugs(client.get("/api/products?sort=price_desc"))[0] == "market-tote"


def test_sort_by_name(client, catalog):
    assert slugs(client.get("/api/products?sort=name")) == [
        "linen-overshirt",
        "market-tote",
        "poplin-shirt",
        "sold-out-shirt",
    ]


# --- other filters ---------------------------------------------------------


def test_in_stock_filter_hides_sold_out_products(client, catalog):
    assert "sold-out-shirt" not in slugs(client.get("/api/products?in_stock=true"))


def test_featured_filter(client, catalog):
    assert slugs(client.get("/api/products?featured=true")) == ["linen-overshirt"]


def test_size_filter_matches_variants(client, catalog):
    assert slugs(client.get("/api/products?size=One size")) == ["market-tote"]


def test_price_bounds(client, catalog):
    assert set(slugs(client.get("/api/products?min_price=3000&max_price=4000"))) == {
        "linen-overshirt",
        "poplin-shirt",
    }


def test_facets_describe_the_current_scope(client, catalog):
    filters = client.get("/api/products?category=shirts").get_json()["data"]["filters"]

    assert filters["sizes"] == ["M", "S"]
    assert filters["colors"] == ["Black", "Bone", "Navy", "Olive"]
    assert filters["price_range"] == {"min": "2800.00", "max": "3600.00"}


def test_facets_ignore_the_narrowing_filters(client, catalog):
    """Choosing a size must not remove the other sizes from the control."""
    unfiltered = client.get("/api/products?category=shirts").get_json()["data"]["filters"]
    filtered = client.get("/api/products?category=shirts&size=S").get_json()["data"][
        "filters"
    ]

    assert filtered["sizes"] == unfiltered["sizes"]


# --- invalid parameters ----------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-3",
        "page=abc",
        "per_page=0",
        "per_page=49",
        "per_page=notanumber",
        "sort=cheapest",
        "min_price=free",
        "min_price=-5",
        "in_stock=maybe",
        "featured=yes-please",
        "q=" + "x" * 200,
    ],
)
def test_invalid_parameters_are_rejected(client, catalog, query):
    response = client.get(f"/api/products?{query}")

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


# --- detail ----------------------------------------------------------------


def test_product_detail_includes_variants_and_options(client, catalog):
    detail = client.get("/api/products/linen-overshirt").get_json()["data"]["product"]

    assert detail["sizes"] == ["M", "S"]
    assert detail["colors"] == ["Bone", "Olive"]
    assert len(detail["variants"]) == 3
    assert detail["in_stock"] is True


def test_variant_exposes_a_purchasable_quantity_not_the_stock_ledger(client, catalog):
    detail = client.get("/api/products/linen-overshirt").get_json()["data"]["product"]
    by_key = {(v["size"], v["color"]): v for v in detail["variants"]}

    assert by_key[("S", "Bone")]["max_quantity"] == 3
    assert by_key[("M", "Olive")]["in_stock"] is False
    assert by_key[("M", "Olive")]["max_quantity"] == 0
    assert "stock_quantity" not in by_key[("S", "Bone")]


def test_unknown_product_is_a_404(client, catalog):
    response = client.get("/api/products/no-such-product")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_images_are_ordered_primary_first(client, catalog):
    detail = client.get("/api/products/linen-overshirt").get_json()["data"]["product"]

    assert [image["alt_text"] for image in detail["images"]]
    assert detail["images"][0]["is_primary"] is True
    assert len(detail["images"]) == 2


def test_images_become_cloudinary_urls_with_a_srcset(client, app, catalog):
    app.config["CLOUDINARY_CLOUD_NAME"] = "test-cloud"

    image = client.get("/api/products/linen-overshirt").get_json()["data"]["product"][
        "images"
    ][0]

    assert image["url"].startswith("https://res.cloudinary.com/test-cloud/image/upload/")
    assert "f_auto,q_auto" in image["url"]
    assert "400w" in image["srcset"] and "1200w" in image["srcset"]


def test_a_product_with_no_images_serialises_cleanly(client, catalog):
    summary = {item["slug"]: item for item in products(client.get("/api/products"))}

    assert summary["poplin-shirt"]["image"] is None
