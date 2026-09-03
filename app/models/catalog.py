"""Catalog: categories, products, variants and images (spec §6, §11)."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, status_enum


class ProductStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class VariantStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    products: Mapped[list["Product"]] = relationship(back_populates="category")

    def __repr__(self) -> str:
        return f"<Category {self.slug}>"


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Products are archived, never hard-deleted, so a category cannot be
    # removed out from under one.
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Money is Numeric, never float. Amounts are KES.
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    status: Mapped[ProductStatus] = mapped_column(
        status_enum(ProductStatus, "product_status"),
        nullable=False,
        default=ProductStatus.DRAFT,
    )
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    category: Mapped[Category] = relationship(back_populates="products")
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.position",
    )

    __table_args__ = (
        CheckConstraint("base_price >= 0", name="ck_products_base_price_non_negative"),
        CheckConstraint(
            "sale_price IS NULL OR sale_price >= 0",
            name="ck_products_sale_price_non_negative",
        ),
        # The shop and homepage both filter on these.
        Index("ix_products_status_featured", "status", "featured"),
    )

    def __repr__(self) -> str:
        return f"<Product {self.slug}>"


class ProductVariant(Base, TimestampMixin):
    __tablename__ = "product_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    size: Mapped[str] = mapped_column(String(32), nullable=False)
    color: Mapped[str] = mapped_column(String(48), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[VariantStatus] = mapped_column(
        status_enum(VariantStatus, "variant_status"),
        nullable=False,
        default=VariantStatus.ACTIVE,
    )

    product: Mapped[Product] = relationship(back_populates="variants")

    __table_args__ = (
        # Business rule 1 lives here as well as in the service layer: the
        # database itself refuses to go oversold.
        CheckConstraint("stock_quantity >= 0", name="ck_variants_stock_non_negative"),
        CheckConstraint("price >= 0", name="ck_variants_price_non_negative"),
        UniqueConstraint("product_id", "size", "color", name="uq_variant_product_size_color"),
    )

    @property
    def in_stock(self) -> bool:
        return self.status == VariantStatus.ACTIVE and self.stock_quantity > 0

    def __repr__(self) -> str:
        return f"<ProductVariant {self.sku}>"


class ProductImage(Base, TimestampMixin):
    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Cloudinary-hosted images store the public_id and derive every URL from it,
    # so sizes and formats can change without a data migration. `url` is the
    # escape hatch for an image hosted somewhere else.
    public_id: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(500))

    alt_text: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    product: Mapped[Product] = relationship(back_populates="images")

    __table_args__ = (
        CheckConstraint(
            "public_id IS NOT NULL OR url IS NOT NULL",
            name="ck_product_images_has_a_source",
        ),
    )

    def __repr__(self) -> str:
        return f"<ProductImage {self.id} product={self.product_id}>"
