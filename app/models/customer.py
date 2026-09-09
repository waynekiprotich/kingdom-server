"""Customer accounts.

Optional, always. Guest checkout remains the primary path and a signed-out
shopper can still buy — an account adds order history and prefilled details,
and never stands between someone and a purchase.

An account is deliberately *not* a way to reach orders placed before it
existed. Orders carry a phone number, not an email, and claiming a stranger's
order history by typing their phone number into a signup form is a data breach
with a friendly name. Orders link to an account from the moment the customer is
signed in at checkout, and an older order is claimed by presenting its
confirmation token — which is already this codebase's proof that you are the
person who placed it.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CredentialMixin, TimestampMixin


class Customer(Base, TimestampMixin, CredentialMixin):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: Optional, and only ever a convenience: it prefills checkout and the
    #: M-Pesa push. It is not verified and it is not an identifier — nothing is
    #: ever looked up or authorised by it.
    phone: Mapped[str | None] = mapped_column(String(20))

    orders: Mapped[list["Order"]] = relationship(  # noqa: F821
        back_populates="customer"
    )

    def __repr__(self) -> str:
        return f"<Customer {self.email}>"
