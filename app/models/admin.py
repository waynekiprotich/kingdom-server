"""Admin accounts (spec §15). Customers never have accounts."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from app.models.base import Base, TimestampMixin, status_enum, utcnow


class AdminRole(StrEnum):
    SUPERADMIN = "superadmin"
    STAFF = "staff"


class Admin(Base, TimestampMixin):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        status_enum(AdminRole, "admin_role"), nullable=False, default=AdminRole.STAFF
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Password handling ---------------------------------------------------
    # Werkzeug's scrypt default; no separate hashing dependency needed.

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    # Brute-force protection (spec §15) -----------------------------------

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

    def __repr__(self) -> str:
        return f"<Admin {self.email}>"
