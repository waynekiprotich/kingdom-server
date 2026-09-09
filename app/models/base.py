"""Declarative base and shared column mixins."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash


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


class CredentialMixin:
    """Password storage and brute-force lockout, for anything that signs in.

    Shared by ``Admin`` and ``Customer`` because the two need exactly the same
    behaviour and a second hand-written copy is a second place for a subtle
    difference to appear — the kind where one of them quietly stops resetting
    the failure count on success.

    Hashing is Werkzeug's scrypt default: no separate hashing dependency.
    """

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        locked_until = self.locked_until
        if locked_until.tzinfo is None:
            # SQLite hands back naive datetimes; treat them as UTC.
            locked_until = locked_until.replace(tzinfo=utcnow().tzinfo)
        return locked_until > utcnow()

    def register_failed_login(self, max_attempts: int, lockout_minutes: int) -> None:
        self.failed_login_count += 1
        if self.failed_login_count >= max_attempts:
            self.locked_until = utcnow() + timedelta(minutes=lockout_minutes)

    def register_successful_login(self) -> None:
        self.failed_login_count = 0
        self.locked_until = None
        self.last_login_at = utcnow()
