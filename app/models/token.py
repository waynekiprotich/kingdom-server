"""Revoked JWTs (spec §15).

Logout has to mean something server-side, otherwise a stolen token stays valid
until it expires. Revoked token ids live here rather than in Redis, which the
stack rules out — the table is small and expired rows are prunable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TokenBlocklist(Base):
    __tablename__ = "token_blocklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jti: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    token_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Whose token this was. Exactly one is set — two nullable foreign keys
    # rather than one polymorphic id column, so the database still enforces
    # that the row points at an account that exists. Revocation works the same
    # for both; only the owner differs.
    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE")
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<TokenBlocklist {self.token_type} {self.jti}>"
