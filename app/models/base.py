"""Declarative base and shared column mixins."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for every model."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def status_enum(enum_class, name: str) -> Enum:
    """A status column stored as VARCHAR with a CHECK constraint.

    Native PostgreSQL enums need a migration to add a value; a checked string
    does not, and reads identically.
    """
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        # SQLAlchemy 2 defaults this to False, which leaves the column a plain
        # VARCHAR that any value can be written into. Ask for the CHECK.
        create_constraint=True,
        length=32,
        values_callable=lambda enum: [member.value for member in enum],
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )
