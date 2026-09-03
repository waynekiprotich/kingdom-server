"""Operational CLI commands."""

from __future__ import annotations

import click
from flask import Flask
from sqlalchemy import select

from app.extensions import db
from app.models.admin import Admin, AdminRole
from app.models.base import utcnow
from app.models.catalog import (
    Category,
    Product,
    ProductImage,
    ProductStatus,
    ProductVariant,
)


def _slugify_option(value: str) -> str:
    return value.lower().replace(" ", "-")


def register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--email", prompt=True, help="Admin email address.")
    @click.option("--name", prompt=True, help="Display name.")
    @click.option(
        "--password",
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
        help="Password. Prompted for, so it never lands in shell history.",
    )
    @click.option(
        "--role",
        type=click.Choice([role.value for role in AdminRole]),
        default=AdminRole.SUPERADMIN.value,
    )
    def create_admin(email: str, name: str, password: str, role: str) -> None:
        """Create an admin account."""
        email = email.strip().lower()

        if db.session.scalar(select(Admin).where(Admin.email == email)):
            raise click.ClickException(f"An admin with email {email} already exists.")

        if len(password) < 12:
            raise click.ClickException("Password must be at least 12 characters.")

        admin = Admin(email=email, name=name.strip(), role=AdminRole(role))
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

        click.echo(f"Created {role} {email}")

    @app.cli.command("seed-dev")
    @click.option(
        "--reset",
        is_flag=True,
        help="Delete existing catalog rows first.",
    )
    def seed_dev(reset: bool) -> None:
        """Load development catalog data.

        Structured stand-in content, not the business's real catalog. Refuses
        to run against production.
        """
        from app.seed_data import CATEGORIES, PRODUCTS, SIZES

        if app.config.get("ENV_NAME") not in ("development", "testing"):
            raise click.ClickException(
                "seed-dev refuses to run outside development. Real catalog data "
                "is entered through the admin dashboard."
            )

        if reset:
            # Order matters: children before parents, and anything holding a
            # RESTRICT reference before the row it points at.
            from app.models.audit import AuditLog
            from app.models.inventory import InventoryMovement
            from app.models.order import Order, OrderItem
            from app.models.payment import Payment

            for model in (
                AuditLog,
                InventoryMovement,
                Payment,
                OrderItem,
                Order,
                ProductImage,
                ProductVariant,
                Product,
                Category,
            ):
                db.session.query(model).delete()
            db.session.commit()
            click.echo("Cleared existing catalog and order rows.")

        categories: dict[str, Category] = {}
        for entry in CATEGORIES:
            category = db.session.scalar(
                select(Category).where(Category.slug == entry["slug"])
            )
            if category is None:
                category = Category(**entry)
                db.session.add(category)
            categories[entry["slug"]] = category
        db.session.flush()

        created = 0
        for entry in PRODUCTS:
            if db.session.scalar(select(Product).where(Product.slug == entry["slug"])):
                continue

            product = Product(
                name=entry["name"],
                slug=entry["slug"],
                description=entry["description"],
                category_id=categories[entry["category"]].id,
                base_price=entry["base_price"],
                sale_price=entry.get("sale_price"),
                status=ProductStatus.ACTIVE,
                featured=entry.get("featured", False),
            )
            db.session.add(product)
            db.session.flush()

            for position, public_id in enumerate(entry["images"]):
                db.session.add(
                    ProductImage(
                        product_id=product.id,
                        public_id=public_id,
                        alt_text=f"{entry['name']} photographed on a plain background",
                        position=position,
                        is_primary=position == 0,
                    )
                )

            sizes = entry.get("sizes", SIZES)
            for color, stock in entry["colors"].items():
                for size in sizes:
                    db.session.add(
                        ProductVariant(
                            product_id=product.id,
                            sku=f"{entry['slug'][:12].upper()}-{_slugify_option(size).upper()}-{color[:3].upper()}",
                            size=size,
                            color=color,
                            price=entry.get("sale_price") or entry["base_price"],
                            stock_quantity=stock,
                        )
                    )
            created += 1

        db.session.commit()

        orders = _seed_orders()

        click.echo(
            f"Seeded {created} development products across {len(categories)} categories."
        )
        if orders:
            click.echo(f"Seeded {orders} development orders.")
        click.echo("This is stand-in data, not real catalog content.")


def _seed_orders() -> int:
    """Development orders, so order management has something to manage.

    Checkout does not exist yet, so these stand in for what it will produce —
    including deducting stock through the ledger, so that cancelling one
    releases exactly what it reserved.
    """
    from decimal import Decimal

    from app.models.inventory import InventoryMovement, MovementReason
    from app.models.order import Order, OrderItem, OrderStatus
    from app.models.payment import Payment, PaymentStatus

    if db.session.scalar(select(Order.id)):
        return 0

    plan = [
        ("KC-100001", OrderStatus.PAYMENT_PENDING, "Achieng Odhiambo", "254712000001", "Kilimani, Nairobi", 1),
        ("KC-100002", OrderStatus.PAID, "Brian Kiptoo", "254712000002", "Westlands, Nairobi", 2),
        ("KC-100003", OrderStatus.PROCESSING, "Cynthia Wambui", "254712000003", "Nakuru CBD", 1),
        ("KC-100004", OrderStatus.SHIPPED, "Daniel Mutiso", "254712000004", "Mombasa, Nyali", 1),
        ("KC-100005", OrderStatus.DELIVERED, "Esther Njeri", "254712000005", "Eldoret", 3),
    ]

    variants = db.session.scalars(
        select(ProductVariant).where(ProductVariant.stock_quantity > 0).limit(10)
    ).all()
    if not variants:
        return 0

    created = 0
    for index, (number, status, name, phone, location, quantity) in enumerate(plan):
        variant = variants[index % len(variants)]
        product = db.session.get(Product, variant.product_id)
        unit_price = variant.price
        line_total = unit_price * quantity
        delivery_fee = Decimal("300.00")

        order = Order(
            order_number=number,
            status=status,
            customer_name=name,
            customer_phone=phone,
            delivery_location=location,
            subtotal=line_total,
            delivery_fee=delivery_fee,
            total=line_total + delivery_fee,
            paid_at=None if status == OrderStatus.PAYMENT_PENDING else utcnow(),
        )
        db.session.add(order)
        db.session.flush()

        db.session.add(
            OrderItem(
                order_id=order.id,
                product_variant_id=variant.id,
                product_name=product.name,
                variant_sku=variant.sku,
                size=variant.size,
                color=variant.color,
                unit_price=unit_price,
                quantity=quantity,
                line_total=line_total,
            )
        )

        # Stock leaves through the ledger, exactly as checkout will do it.
        taken = min(quantity, variant.stock_quantity)
        variant.stock_quantity -= taken
        db.session.add(
            InventoryMovement(
                product_variant_id=variant.id,
                delta=-taken,
                reason=MovementReason.SALE,
                order_id=order.id,
                note=f"Sold on {number}",
            )
        )

        if status != OrderStatus.PAYMENT_PENDING:
            db.session.add(
                Payment(
                    order_id=order.id,
                    status=PaymentStatus.SUCCESSFUL,
                    amount=order.total,
                    phone_number=phone,
                    mpesa_receipt_number=f"DEV{number[-6:]}",
                    result_code=0,
                    result_desc="The service request is processed successfully.",
                    transaction_date=utcnow(),
                )
            )
        created += 1

    db.session.commit()
    return created
