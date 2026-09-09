"""Admin accounts (spec §15).

Customers may now hold accounts too (``models/customer.py``), but they are a
*different table with a different token type* — see ``app/authz.py``. Nothing a
customer can sign into grants anything here.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CredentialMixin, TimestampMixin, status_enum


class AdminRole(StrEnum):
    SUPERADMIN = "superadmin"
    STAFF = "staff"


class Admin(Base, TimestampMixin, CredentialMixin):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        status_enum(AdminRole, "admin_role"), nullable=False, default=AdminRole.STAFF
    )

    def __repr__(self) -> str:
        return f"<Admin {self.email}>"
