"""Admin catalog management (spec §6, §13, §19).

Everything here is behind an active admin session. Two rules run through the
whole module:

* **Every mutation writes an audit row** (§19) in the same transaction as the
  change, so the trail cannot disagree with the data.
* **Stock only moves through an inventory movement** (§19), so "why is this
  variant at 3?" always has an answer.
"""

from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, g, jsonify
from sqlalchemy import func, or_, select

from app.authz import admin_required
from app.errors import ConflictError, NotFoundError, ValidationError
from app.extensions import db
from app.models.admin import AdminRole
from app.models.catalog import (
    Category,
    Product,
    ProductImage,
    ProductStatus,
    ProductVariant,
    VariantStatus,
)
from app.models.inventory import InventoryMovement, MovementReason
from app.models.order import OrderItem
from app.serializers import (
    serialize_admin_category,
    serialize_admin_image,
    serialize_admin_product,
    serialize_admin_product_summary,
    serialize_admin_variant,
)
from app.services import audit
from app.utils import slugify
from app.validation import (
    MISSING,
    body_bool,
    body_choice,
    body_decimal,
    body_int,
    body_str,
    json_body,
    query_choice,
    query_int,
    query_str,
    reject_unknown_fields,
)

bp = Blueprint("admin_catalog", __name__, url_prefix="/api/admin")

PRODUCT_STATUSES = tuple(status.value for status in ProductStatus)
VARIANT_STATUSES = tuple(status.value for status in VariantStatus)


# --- helpers ---------------------------------------------------------------


def _get_or_404(model, entity_id: int, code: str, label: str):
    instance = db.session.get(model, entity_id)
    if instance is None:
        raise NotFoundError(f"No {label} with id {entity_id}.", code=code)
    return instance


def _unique_slug(base: str, *, exclude_id: int | None = None) -> str:
    """Append a counter until the slug is free.

    Used only for slugs we generate. A slug the admin typed is never silently
    altered — that returns a conflict instead.
    """
    candidate = base or "item"
    suffix = 1

    while True:
        query = select(Product.id).where(Product.slug == candidate)
        if exclude_id is not None:
            query = query.where(Product.id != exclude_id)
        if db.session.scalar(query) is None:
            return candidate
        suffix += 1
        candidate = f"{base}-{suffix}"


def _require_free_slug(model, slug: str, *, exclude_id: int | None = None) -> str:
    query = select(model.id).where(model.slug == slug)
    if exclude_id is not None:
        query = query.where(model.id != exclude_id)
    if db.session.scalar(query) is not None:
        raise ConflictError(
            f"The slug '{slug}' is already in use.", code="SLUG_TAKEN"
        )
    return slug


def _resolve_category(category_id: int) -> Category:
    return _get_or_404(Category, category_id, "CATEGORY_NOT_FOUND", "category")


def _adjust_stock(variant: ProductVariant, new_quantity: int, note: str) -> None:
    """Move stock and record why, together.

    Writing ``stock_quantity`` without a movement row would leave the ledger
    lying, so the two happen in one place.
    """
    delta = new_quantity - variant.stock_quantity
    if delta == 0:
        return

    variant.stock_quantity = new_quantity
    db.session.add(
        InventoryMovement(
            product_variant_id=variant.id,
            delta=delta,
            reason=MovementReason.ADJUSTMENT,
            admin_id=g.admin.id,
            note=note,
        )
    )


# --- categories ------------------------------------------------------------

CATEGORY_FIELDS = ("name", "slug", "description", "position")


@bp.get("/categories")
@admin_required()
def list_categories():
    counts = (
        select(Product.category_id, func.count(Product.id).label("total"))
        .group_by(Product.category_id)
        .subquery()
    )
    rows = db.session.execute(
        select(Category, func.coalesce(counts.c.total, 0))
        .outerjoin(counts, counts.c.category_id == Category.id)
        .order_by(Category.position, Category.name)
    ).all()

    return jsonify(
        {
            "success": True,
            "data": {
                "categories": [
                    serialize_admin_category(category, product_count=count)
                    for category, count in rows
                ]
            },
        }
    )


@bp.post("/categories")
@admin_required()
def create_category():
    body = json_body()
    reject_unknown_fields(body, CATEGORY_FIELDS)

    name = body_str(body, "name", required=True, max_length=120)
    slug = body_str(body, "slug", max_length=140)
    description = body_str(body, "description", max_length=2000, nullable=True)
    position = body_int(body, "position", minimum=0, maximum=9999)

    slug = _require_free_slug(Category, slug if slug is not MISSING else slugify(name))

    category = Category(
        name=name,
        slug=slug,
        description=None if description is MISSING else description,
        position=0 if position is MISSING else position,
    )
    db.session.add(category)
    db.session.flush()

    audit.record("category.create", "category", category.id, changes={
        "name": {"from": None, "to": name},
        "slug": {"from": None, "to": slug},
    })
    db.session.commit()

    return jsonify({"success": True, "data": {"category": serialize_admin_category(category)}}), 201


@bp.get("/categories/<int:category_id>")
@admin_required()
def get_category(category_id: int):
    category = _resolve_category(category_id)
    return jsonify({"success": True, "data": {"category": serialize_admin_category(category)}})


@bp.patch("/categories/<int:category_id>")
@admin_required()
def update_category(category_id: int):
    category = _resolve_category(category_id)
    body = json_body()
    reject_unknown_fields(body, CATEGORY_FIELDS)

    slug = body_str(body, "slug", max_length=140)
    if slug is not MISSING:
        _require_free_slug(Category, slug, exclude_id=category.id)

    changes = audit.apply_changes(
        category,
        {
            "name": body_str(body, "name", max_length=120),
            "slug": slug,
            "description": body_str(body, "description", max_length=2000, nullable=True),
            "position": body_int(body, "position", minimum=0, maximum=9999),
        },
    )

    if changes:
        audit.record("category.update", "category", category.id, changes=changes)
        db.session.commit()

    return jsonify(
        {
            "success": True,
            "data": {"category": serialize_admin_category(category), "changed": list(changes)},
        }
    )


@bp.delete("/categories/<int:category_id>")
@admin_required(AdminRole.SUPERADMIN)
def delete_category(category_id: int):
    category = _resolve_category(category_id)

    in_use = db.session.scalar(
        select(func.count(Product.id)).where(Product.category_id == category.id)
    )
    if in_use:
        raise ConflictError(
            f"{category.name} still has {in_use} product(s). Move or archive them first.",
            code="CATEGORY_NOT_EMPTY",
        )

    audit.record("category.delete", "category", category.id, changes={
        "name": {"from": category.name, "to": None},
    })
    db.session.delete(category)
    db.session.commit()

    return jsonify({"success": True, "data": {"deleted": category_id}})


# --- products --------------------------------------------------------------

PRODUCT_FIELDS = (
    "name",
    "slug",
    "description",
    "category_id",
    "base_price",
    "sale_price",
    "status",
    "featured",
)


@bp.get("/products")
@admin_required()
def list_products():
    """Unlike the public listing, this shows drafts and archived products."""
    page = query_int("page", default=1, minimum=1, maximum=10_000)
    per_page = query_int("per_page", default=25, minimum=1, maximum=100)
    status = query_str("status", max_length=20)
    search = query_str("q")
    category_id = query_int("category_id", default=0, minimum=0, maximum=2**31)
    sort = query_choice("sort", ("newest", "name", "updated"), default="newest")

    filters = []
    if status:
        if status not in PRODUCT_STATUSES:
            raise ValidationError(
                f"status must be one of: {', '.join(PRODUCT_STATUSES)}."
            )
        filters.append(Product.status == ProductStatus(status))
    if category_id:
        filters.append(Product.category_id == category_id)
    if search:
        term = f"%{search}%"
        filters.append(or_(Product.name.ilike(term), Product.slug.ilike(term)))

    ordering = {
        "newest": Product.created_at.desc(),
        "name": Product.name.asc(),
        "updated": Product.updated_at.desc(),
    }[sort]

    total = db.session.scalar(select(func.count()).select_from(Product).where(*filters))
    products = db.session.scalars(
        select(Product)
        .where(*filters)
        .order_by(ordering, Product.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    total_pages = (total + per_page - 1) // per_page if total else 0

    return jsonify(
        {
            "success": True,
            "data": {
                "products": [serialize_admin_product_summary(p) for p in products],
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "total_pages": total_pages,
                    "has_prev": page > 1,
                    "has_next": page < total_pages,
                },
            },
        }
    )


@bp.post("/products")
@admin_required()
def create_product():
    body = json_body()
    reject_unknown_fields(body, PRODUCT_FIELDS)

    name = body_str(body, "name", required=True, max_length=200)
    category_id = body_int(body, "category_id", required=True, minimum=1)
    base_price = body_decimal(body, "base_price", required=True, minimum=Decimal("0"))
    sale_price = body_decimal(body, "sale_price", nullable=True, minimum=Decimal("0"))
    description = body_str(body, "description", max_length=5000)
    status = body_choice(body, "status", PRODUCT_STATUSES)
    featured = body_bool(body, "featured")
    slug = body_str(body, "slug", max_length=220)

    _resolve_category(category_id)

    if sale_price not in (MISSING, None) and sale_price >= base_price:
        raise ValidationError("sale_price must be below base_price.")

    slug = (
        _require_free_slug(Product, slug)
        if slug is not MISSING
        else _unique_slug(slugify(name))
    )

    product = Product(
        name=name,
        slug=slug,
        description="" if description is MISSING else description,
        category_id=category_id,
        base_price=base_price,
        sale_price=None if sale_price is MISSING else sale_price,
        status=ProductStatus.DRAFT if status is MISSING else ProductStatus(status),
        featured=False if featured is MISSING else featured,
    )
    db.session.add(product)
    db.session.flush()

    audit.record("product.create", "product", product.id, changes={
        "name": {"from": None, "to": name},
        "slug": {"from": None, "to": slug},
        "status": {"from": None, "to": product.status.value},
    })
    db.session.commit()

    return jsonify({"success": True, "data": {"product": serialize_admin_product(product)}}), 201


@bp.get("/products/<int:product_id>")
@admin_required()
def get_product(product_id: int):
    product = _get_or_404(Product, product_id, "PRODUCT_NOT_FOUND", "product")
    return jsonify({"success": True, "data": {"product": serialize_admin_product(product)}})


@bp.patch("/products/<int:product_id>")
@admin_required()
def update_product(product_id: int):
    product = _get_or_404(Product, product_id, "PRODUCT_NOT_FOUND", "product")
    body = json_body()
    reject_unknown_fields(body, PRODUCT_FIELDS)

    slug = body_str(body, "slug", max_length=220)
    if slug is not MISSING:
        _require_free_slug(Product, slug, exclude_id=product.id)

    category_id = body_int(body, "category_id", minimum=1)
    if category_id is not MISSING:
        _resolve_category(category_id)

    base_price = body_decimal(body, "base_price", minimum=Decimal("0"))
    sale_price = body_decimal(body, "sale_price", nullable=True, minimum=Decimal("0"))

    # Compare against whichever value will be in force after this request.
    effective_base = product.base_price if base_price is MISSING else base_price
    effective_sale = product.sale_price if sale_price is MISSING else sale_price
    if effective_sale is not None and effective_sale >= effective_base:
        raise ValidationError("sale_price must be below base_price.")

    status = body_choice(body, "status", PRODUCT_STATUSES)

    changes = audit.apply_changes(
        product,
        {
            "name": body_str(body, "name", max_length=200),
            "slug": slug,
            "description": body_str(body, "description", max_length=5000),
            "category_id": category_id,
            "base_price": base_price,
            "sale_price": sale_price,
            "status": MISSING if status is MISSING else ProductStatus(status),
            "featured": body_bool(body, "featured"),
        },
    )

    if changes:
        audit.record("product.update", "product", product.id, changes=changes)
        db.session.commit()

    return jsonify(
        {
            "success": True,
            "data": {"product": serialize_admin_product(product), "changed": list(changes)},
        }
    )


@bp.delete("/products/<int:product_id>")
@admin_required()
def delete_product(product_id: int):
    """Archive by default; permanent deletion is superadmin-only and refused
    once the product has been ordered (order history must stay intact)."""
    product = _get_or_404(Product, product_id, "PRODUCT_NOT_FOUND", "product")
    permanent = (query_str("permanent") or "").lower() == "true"

    if not permanent:
        if product.status == ProductStatus.ARCHIVED:
            return jsonify(
                {"success": True, "data": {"product": serialize_admin_product(product)}}
            )

        changes = audit.apply_changes(product, {"status": ProductStatus.ARCHIVED})
        audit.record("product.archive", "product", product.id, changes=changes)
        db.session.commit()
        return jsonify(
            {"success": True, "data": {"product": serialize_admin_product(product)}}
        )

    if str(g.admin.role) != AdminRole.SUPERADMIN.value:
        raise ConflictError(
            "Only a superadmin can permanently delete a product. Archive it instead.",
            code="INSUFFICIENT_ROLE",
        )

    ordered = db.session.scalar(
        select(func.count(OrderItem.id))
        .join(ProductVariant, OrderItem.product_variant_id == ProductVariant.id)
        .where(ProductVariant.product_id == product.id)
    )
    if ordered:
        raise ConflictError(
            "This product appears in existing orders and cannot be deleted. "
            "Archive it instead.",
            code="PRODUCT_HAS_ORDERS",
        )

    audit.record("product.delete", "product", product.id, changes={
        "name": {"from": product.name, "to": None},
    })
    db.session.delete(product)
    db.session.commit()

    return jsonify({"success": True, "data": {"deleted": product_id}})


# --- variants --------------------------------------------------------------

VARIANT_FIELDS = ("sku", "size", "color", "price", "stock_quantity", "status")


@bp.post("/products/<int:product_id>/variants")
@admin_required()
def create_variant(product_id: int):
    product = _get_or_404(Product, product_id, "PRODUCT_NOT_FOUND", "product")
    body = json_body()
    reject_unknown_fields(body, VARIANT_FIELDS)

    size = body_str(body, "size", required=True, max_length=32)
    color = body_str(body, "color", required=True, max_length=48)
    price = body_decimal(body, "price", required=True, minimum=Decimal("0"))
    sku = body_str(body, "sku", max_length=64)
    stock = body_int(body, "stock_quantity", minimum=0, maximum=1_000_000)
    status = body_choice(body, "status", VARIANT_STATUSES)

    if db.session.scalar(
        select(ProductVariant.id).where(
            ProductVariant.product_id == product.id,
            ProductVariant.size == size,
            ProductVariant.color == color,
        )
    ):
        raise ConflictError(
            f"{product.name} already has a {size} / {color} variant.",
            code="VARIANT_EXISTS",
        )

    if sku is MISSING:
        # rstrip("-") because truncating a slug can land on a hyphen, which
        # would otherwise give "MERINO-CREW--M-CHA".
        stem = slugify(product.slug)[:12].rstrip("-").upper()
        sku = f"{stem}-{slugify(size).upper()}-{color[:3].upper()}"

    if db.session.scalar(select(ProductVariant.id).where(ProductVariant.sku == sku)):
        raise ConflictError(f"SKU '{sku}' is already in use.", code="SKU_TAKEN")

    opening_stock = 0 if stock is MISSING else stock

    variant = ProductVariant(
        product_id=product.id,
        sku=sku,
        size=size,
        color=color,
        price=price,
        stock_quantity=0,
        status=VariantStatus.ACTIVE if status is MISSING else VariantStatus(status),
    )
    db.session.add(variant)
    db.session.flush()

    # Even the opening stock goes through the ledger, so the movements always
    # sum to the quantity on hand.
    _adjust_stock(variant, opening_stock, note="Opening stock")

    audit.record("variant.create", "variant", variant.id, changes={
        "sku": {"from": None, "to": sku},
        "stock_quantity": {"from": 0, "to": opening_stock},
    })
    db.session.commit()

    return jsonify({"success": True, "data": {"variant": serialize_admin_variant(variant)}}), 201


@bp.patch("/variants/<int:variant_id>")
@admin_required()
def update_variant(variant_id: int):
    variant = _get_or_404(ProductVariant, variant_id, "VARIANT_NOT_FOUND", "variant")
    body = json_body()
    reject_unknown_fields(body, VARIANT_FIELDS)

    sku = body_str(body, "sku", max_length=64)
    if sku is not MISSING and db.session.scalar(
        select(ProductVariant.id).where(
            ProductVariant.sku == sku, ProductVariant.id != variant.id
        )
    ):
        raise ConflictError(f"SKU '{sku}' is already in use.", code="SKU_TAKEN")

    status = body_choice(body, "status", VARIANT_STATUSES)
    stock = body_int(body, "stock_quantity", minimum=0, maximum=1_000_000)

    changes = audit.apply_changes(
        variant,
        {
            "sku": sku,
            "size": body_str(body, "size", max_length=32),
            "color": body_str(body, "color", max_length=48),
            "price": body_decimal(body, "price", minimum=Decimal("0")),
            "status": MISSING if status is MISSING else VariantStatus(status),
        },
    )

    if stock is not MISSING and stock != variant.stock_quantity:
        changes["stock_quantity"] = {"from": variant.stock_quantity, "to": stock}
        _adjust_stock(variant, stock, note="Adjusted from the admin dashboard")

    if changes:
        audit.record("variant.update", "variant", variant.id, changes=changes)
        db.session.commit()

    return jsonify(
        {
            "success": True,
            "data": {"variant": serialize_admin_variant(variant), "changed": list(changes)},
        }
    )


@bp.delete("/variants/<int:variant_id>")
@admin_required()
def delete_variant(variant_id: int):
    """Sold variants are deactivated, never deleted — an order line points at
    them and that history has to survive."""
    variant = _get_or_404(ProductVariant, variant_id, "VARIANT_NOT_FOUND", "variant")

    ordered = db.session.scalar(
        select(func.count(OrderItem.id)).where(
            OrderItem.product_variant_id == variant.id
        )
    )

    if ordered:
        changes = audit.apply_changes(variant, {"status": VariantStatus.INACTIVE})
        audit.record("variant.deactivate", "variant", variant.id, changes=changes)
        db.session.commit()
        return jsonify(
            {
                "success": True,
                "data": {
                    "variant": serialize_admin_variant(variant),
                    "deactivated": True,
                    "reason": "This variant appears in existing orders.",
                },
            }
        )

    audit.record("variant.delete", "variant", variant.id, changes={
        "sku": {"from": variant.sku, "to": None},
    })
    db.session.delete(variant)
    db.session.commit()

    return jsonify({"success": True, "data": {"deleted": variant_id}})


# --- images ----------------------------------------------------------------

IMAGE_FIELDS = ("public_id", "url", "alt_text", "position", "is_primary")


def _clear_other_primaries(product_id: int, keep_id: int) -> None:
    """Exactly one image per product is the primary one."""
    for other in db.session.scalars(
        select(ProductImage).where(
            ProductImage.product_id == product_id,
            ProductImage.is_primary.is_(True),
            ProductImage.id != keep_id,
        )
    ):
        other.is_primary = False


@bp.post("/products/<int:product_id>/images")
@admin_required()
def add_image(product_id: int):
    """Attach an already-uploaded image.

    The file itself went straight to Cloudinary from the browser using a
    signature from /api/admin/images/upload-signature; what arrives here is the
    resulting public_id.
    """
    product = _get_or_404(Product, product_id, "PRODUCT_NOT_FOUND", "product")
    body = json_body()
    reject_unknown_fields(body, IMAGE_FIELDS)

    public_id = body_str(body, "public_id", max_length=255)
    url = body_str(body, "url", max_length=500)
    alt_text = body_str(body, "alt_text", max_length=255)
    position = body_int(body, "position", minimum=0, maximum=999)
    is_primary = body_bool(body, "is_primary")

    if public_id is MISSING and url is MISSING:
        raise ValidationError("Provide either public_id or url.")

    if alt_text is MISSING or not alt_text:
        raise ValidationError(
            "alt_text is required — every product image needs a description."
        )

    if position is MISSING:
        highest = db.session.scalar(
            select(func.max(ProductImage.position)).where(
                ProductImage.product_id == product.id
            )
        )
        position = 0 if highest is None else highest + 1

    # The first image on a product is its primary unless told otherwise.
    has_existing = db.session.scalar(
        select(func.count(ProductImage.id)).where(ProductImage.product_id == product.id)
    )
    primary = (not has_existing) if is_primary is MISSING else is_primary

    image = ProductImage(
        product_id=product.id,
        public_id=None if public_id is MISSING else public_id,
        url=None if url is MISSING else url,
        alt_text=alt_text,
        position=position,
        is_primary=primary,
    )
    db.session.add(image)
    db.session.flush()

    if primary:
        _clear_other_primaries(product.id, image.id)

    audit.record("image.create", "image", image.id, changes={
        "public_id": {"from": None, "to": image.public_id},
        "product_id": {"from": None, "to": product.id},
    })
    db.session.commit()

    return jsonify({"success": True, "data": {"image": serialize_admin_image(image)}}), 201


@bp.patch("/images/<int:image_id>")
@admin_required()
def update_image(image_id: int):
    image = _get_or_404(ProductImage, image_id, "IMAGE_NOT_FOUND", "image")
    body = json_body()
    reject_unknown_fields(body, IMAGE_FIELDS)

    alt_text = body_str(body, "alt_text", max_length=255)
    if alt_text is not MISSING and not alt_text:
        raise ValidationError("alt_text cannot be empty.")

    is_primary = body_bool(body, "is_primary")

    changes = audit.apply_changes(
        image,
        {
            "public_id": body_str(body, "public_id", max_length=255, nullable=True),
            "url": body_str(body, "url", max_length=500, nullable=True),
            "alt_text": alt_text,
            "position": body_int(body, "position", minimum=0, maximum=999),
            "is_primary": is_primary,
        },
    )

    if image.public_id is None and image.url is None:
        raise ValidationError("An image needs either a public_id or a url.")

    if is_primary is True:
        _clear_other_primaries(image.product_id, image.id)

    if changes:
        audit.record("image.update", "image", image.id, changes=changes)
        db.session.commit()

    return jsonify(
        {
            "success": True,
            "data": {"image": serialize_admin_image(image), "changed": list(changes)},
        }
    )


@bp.delete("/images/<int:image_id>")
@admin_required()
def delete_image(image_id: int):
    image = _get_or_404(ProductImage, image_id, "IMAGE_NOT_FOUND", "image")
    product_id, was_primary = image.product_id, image.is_primary

    audit.record("image.delete", "image", image.id, changes={
        "public_id": {"from": image.public_id, "to": None},
    })
    db.session.delete(image)
    db.session.flush()

    # A product should not be left with photographs but no primary one.
    if was_primary:
        replacement = db.session.scalar(
            select(ProductImage)
            .where(ProductImage.product_id == product_id)
            .order_by(ProductImage.position)
            .limit(1)
        )
        if replacement is not None:
            replacement.is_primary = True

    db.session.commit()

    return jsonify({"success": True, "data": {"deleted": image_id}})
