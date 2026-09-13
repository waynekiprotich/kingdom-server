"""Public catalog endpoints (spec §6, §13, §20).

Only ``active`` products are visible here. Drafts and archived products exist
in the database and never leave it through this blueprint.
"""

from __future__ import annotations

import time
from threading import Lock

from flask import Blueprint, current_app, jsonify
from sqlalchemy import and_, asc, desc, exists, func, or_, select
from sqlalchemy.orm import selectinload

from app.errors import NotFoundError
from app.extensions import db
from app.models.catalog import (
    Category,
    Product,
    ProductStatus,
    ProductVariant,
    VariantStatus,
)
from app.serializers import (
    money,
    serialize_category,
    serialize_product_detail,
    serialize_product_summary,
)
from app.validation import (
    like_pattern,
    query_bool,
    query_choice,
    query_decimal,
    query_int,
    query_str,
)

bp = Blueprint("catalog", __name__, url_prefix="/api")

DEFAULT_PER_PAGE = 24
MAX_PER_PAGE = 48

#: Sort keys the storefront may ask for, mapped to their ordering. Anything
#: else is a validation error rather than a silent fallback, so a typo in the
#: frontend surfaces immediately.
SORT_OPTIONS = ("newest", "price_asc", "price_desc", "name")


def _effective_price():
    """The price a customer actually pays: the sale price when one is set."""
    return func.coalesce(Product.sale_price, Product.base_price)


def _has_stock():
    """EXISTS(...) rather than loading every variant to count in Python."""
    return exists().where(
        and_(
            ProductVariant.product_id == Product.id,
            ProductVariant.status == VariantStatus.ACTIVE,
            ProductVariant.stock_quantity > 0,
        )
    )


def _ordering(sort: str):
    if sort == "price_asc":
        return (asc(_effective_price()), asc(Product.id))
    if sort == "price_desc":
        return (desc(_effective_price()), asc(Product.id))
    if sort == "name":
        return (asc(Product.name), asc(Product.id))
    # newest: id breaks ties so pagination is stable when timestamps collide.
    return (desc(Product.created_at), desc(Product.id))


def _resolve_category(slug: str | None) -> Category | None:
    if slug is None:
        return None
    category = db.session.scalar(select(Category).where(Category.slug == slug))
    if category is None:
        raise NotFoundError(
            f"No category matches '{slug}'.", code="CATEGORY_NOT_FOUND"
        )
    return category


#: The cache is per process, so each gunicorn worker warms its own. That is
#: the right trade at this size: a shared cache server would be a new piece of
#: infrastructure to run and pay for, to save a query that already only runs
#: once a minute per worker.
#:
#: Bounded, because the scope key contains the caller's search term. An
#: unbounded dict keyed on attacker-supplied text is a memory-exhaustion bug,
#: not a cache.
FACET_CACHE_MAX_ENTRIES = 256

_facet_cache: dict[tuple[int | None, str | None], tuple[float, dict]] = {}
_facet_cache_lock = Lock()


def _facets(base_filters, scope_key: tuple[int | None, str | None]) -> dict:
    """Facets for this scope, computed at most once per ``FACET_CACHE_SECONDS``.

    ``scope_key`` must identify exactly what ``base_filters`` select — the
    category and the search term — because that pair is what the answer
    depends on.
    """
    ttl = current_app.config["FACET_CACHE_SECONDS"]
    if ttl <= 0:
        return _compute_facets(base_filters)

    now = time.monotonic()

    with _facet_cache_lock:
        cached = _facet_cache.get(scope_key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]

    # Computed outside the lock: this is the slow part, and holding a lock
    # across it would serialise every catalog request behind one query.
    # Two callers racing here both compute and the second simply overwrites,
    # which is cheaper than the contention avoiding it would cost.
    computed = _compute_facets(base_filters)

    with _facet_cache_lock:
        _facet_cache[scope_key] = (now, computed)
        if len(_facet_cache) > FACET_CACHE_MAX_ENTRIES:
            # Drop the oldest entries. The hot scopes — no filter, and one per
            # category — are re-warmed on their next request; what gets
            # evicted is the long tail of one-off search terms.
            for key in sorted(_facet_cache, key=lambda k: _facet_cache[k][0])[
                : len(_facet_cache) - FACET_CACHE_MAX_ENTRIES
            ]:
                del _facet_cache[key]

    return computed


def _compute_facets(base_filters) -> dict:
    """Which sizes, colours and prices exist within the current scope.

    Deliberately ignores the size/colour/price filters themselves, so choosing
    "M" does not make every other size disappear from the control.
    """
    scoped_products = select(Product.id).where(*base_filters).subquery()

    options = db.session.execute(
        select(ProductVariant.size, ProductVariant.color)
        .where(
            ProductVariant.product_id.in_(select(scoped_products.c.id)),
            ProductVariant.status == VariantStatus.ACTIVE,
        )
        .distinct()
    ).all()

    price_range = db.session.execute(
        select(func.min(_effective_price()), func.max(_effective_price())).where(
            *base_filters
        )
    ).one()

    return {
        "sizes": sorted({row.size for row in options}),
        "colors": sorted({row.color for row in options}),
        "price_range": {"min": money(price_range[0]), "max": money(price_range[1])},
    }


@bp.get("/products")
def list_products():
    page = query_int("page", default=1, minimum=1, maximum=10_000)
    per_page = query_int(
        "per_page", default=DEFAULT_PER_PAGE, minimum=1, maximum=MAX_PER_PAGE
    )
    sort = query_choice("sort", SORT_OPTIONS, default="newest")

    search = query_str("q")
    size = query_str("size", max_length=32)
    color = query_str("color", max_length=48)
    min_price = query_decimal("min_price")
    max_price = query_decimal("max_price")
    in_stock_only = query_bool("in_stock")
    featured_only = query_bool("featured")
    category = _resolve_category(query_str("category", max_length=140))

    # Scope: what the category/search context contains, before the narrowing
    # filters. Facets are computed against this so the controls stay stable.
    base_filters = [Product.status == ProductStatus.ACTIVE]
    if category is not None:
        base_filters.append(Product.category_id == category.id)
    if search:
        term = like_pattern(search)
        base_filters.append(
            or_(
                Product.name.ilike(term, escape="\\"),
                Product.description.ilike(term, escape="\\"),
            )
        )

    filters = list(base_filters)
    if size:
        filters.append(
            exists().where(
                and_(
                    ProductVariant.product_id == Product.id,
                    ProductVariant.status == VariantStatus.ACTIVE,
                    ProductVariant.size == size,
                )
            )
        )
    if color:
        filters.append(
            exists().where(
                and_(
                    ProductVariant.product_id == Product.id,
                    ProductVariant.status == VariantStatus.ACTIVE,
                    ProductVariant.color == color,
                )
            )
        )
    if min_price is not None:
        filters.append(_effective_price() >= min_price)
    if max_price is not None:
        filters.append(_effective_price() <= max_price)
    if in_stock_only:
        filters.append(_has_stock())
    if featured_only is not None:
        filters.append(Product.featured.is_(featured_only))

    total = db.session.scalar(
        select(func.count()).select_from(Product).where(*filters)
    )

    rows = db.session.execute(
        select(Product, _has_stock().label("in_stock"))
        .where(*filters)
        .order_by(*_ordering(sort))
        .limit(per_page)
        .offset((page - 1) * per_page)
        # One extra query each rather than one per product (§22).
        .options(
            selectinload(Product.images),
            selectinload(Product.category),
        )
    ).all()

    total_pages = (total + per_page - 1) // per_page if total else 0

    return jsonify(
        {
            "success": True,
            "data": {
                "products": [
                    serialize_product_summary(product, in_stock=in_stock)
                    for product, in_stock in rows
                ],
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "total_pages": total_pages,
                    "has_prev": page > 1,
                    "has_next": page < total_pages,
                },
                "filters": {
                    "category": serialize_category(category) if category else None,
                    "sort": sort,
                    "query": search,
                    **_facets(
                        base_filters,
                        (category.id if category else None, search or None),
                    ),
                },
            },
        }
    )


@bp.get("/products/<slug>")
def get_product(slug: str):
    product = db.session.scalar(
        select(Product)
        .where(Product.slug == slug, Product.status == ProductStatus.ACTIVE)
        .options(
            selectinload(Product.images),
            selectinload(Product.variants),
            selectinload(Product.category),
        )
    )

    if product is None:
        raise NotFoundError(
            "That product is not available.", code="PRODUCT_NOT_FOUND"
        )

    return jsonify({"success": True, "data": {"product": serialize_product_detail(product)}})


@bp.get("/categories")
def list_categories():
    """Categories that have something to show, with live product counts."""
    counts = (
        select(
            Product.category_id.label("category_id"),
            func.count(Product.id).label("product_count"),
        )
        .where(Product.status == ProductStatus.ACTIVE)
        .group_by(Product.category_id)
        .subquery()
    )

    rows = db.session.execute(
        select(Category, func.coalesce(counts.c.product_count, 0))
        .outerjoin(counts, counts.c.category_id == Category.id)
        .order_by(Category.position, Category.name)
    ).all()

    return jsonify(
        {
            "success": True,
            "data": {
                "categories": [
                    serialize_category(category, product_count=count)
                    for category, count in rows
                ]
            },
        }
    )
