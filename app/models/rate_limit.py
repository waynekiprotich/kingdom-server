"""Rate limit counters (spec §16).

Kept in PostgreSQL rather than in memory because the API runs under gunicorn
with several workers, and a per-process counter would let a caller have N
times the intended allowance simply by being load-balanced around. Redis is
the usual answer and is on the stack's "do not introduce" list, so the
database we already run does the job: one row per (bucket, window), bumped
with a single atomic UPSERT.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"

    #: What is being limited — "login:203.0.113.4", "orders:203.0.113.4", and
    #: so on. The caller builds it; this table does not care about the shape.
    bucket: Mapped[str] = mapped_column(String(160), primary_key=True)

    #: Start of the fixed window this count belongs to. Part of the key, so a
    #: new window is a new row and expiry is just "ignore older rows".
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )

    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("hits >= 0", name="ck_rate_limit_hits_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<RateLimitCounter {self.bucket} {self.hits}>"
