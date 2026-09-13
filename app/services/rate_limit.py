"""Fixed-window rate limiting (spec §16).

Guards the endpoints where repetition is the attack: signing in, placing
orders, minting upload signatures. The public catalog is deliberately not
limited — it is read-only, cached, and the cost of a database write per
product listing would be worse than the abuse it prevents.

Two decisions worth knowing about:

Counts are written on their own connection, not the request's session. A
failed login rolls its session back, and if the counter rode along it would
roll back too — leaving the one case we most want to count uncounted.

The limiter fails open. If the table is missing (a deploy that forgot the
migration) or the database is briefly unreachable, requests are allowed and
the problem is logged. A limiter that takes the site down when it breaks is
worse than the abuse it was added to stop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import current_app, request
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from app.errors import RateLimitError
from app.extensions import db
from app.models.rate_limit import RateLimitCounter

logger = logging.getLogger(__name__)


def client_ip() -> str:
    """The caller's address, as far as it can be trusted.

    ``remote_addr`` is only meaningful once ProxyFix has rewritten it from the
    hops the platform appends; see ``TRUSTED_PROXY_HOPS``. Without that it is
    the load balancer's own address, which would put every visitor in one
    bucket — hence the fallback string rather than a crash, so a
    misconfiguration is visible in the data instead of silently keyed on None.
    """
    return request.remote_addr or "unknown"


def _window_start(now: datetime, seconds: int) -> datetime:
    """Floor ``now`` to the start of its window."""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


def _upsert(connection, bucket: str, window: datetime) -> int:
    """Add one to this window's count and return the new total.

    One statement, so two workers racing on the same bucket cannot both read
    the same count and write it back.
    """
    dialect = connection.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - only these two are supported
        raise RuntimeError(f"No UPSERT support wired up for {dialect}.")

    statement = (
        insert(RateLimitCounter)
        .values(bucket=bucket, window_start=window, hits=1)
        .on_conflict_do_update(
            index_elements=["bucket", "window_start"],
            set_={"hits": RateLimitCounter.hits + 1},
        )
        .returning(RateLimitCounter.hits)
    )
    return connection.execute(statement).scalar_one()


def hit(bucket: str, limit: int, window_seconds: int) -> int | None:
    """Record one use of ``bucket``; return seconds to wait if it is spent.

    ``None`` means the caller is inside their allowance and the request should
    proceed.
    """
    now = datetime.now(timezone.utc)
    window = _window_start(now, window_seconds)

    try:
        with db.engine.begin() as connection:
            hits = _upsert(connection, bucket, window)

            # Windows that have already closed are dead weight. Clearing them
            # for this bucket keeps the table proportional to active callers
            # rather than to all traffic ever seen.
            connection.execute(
                delete(RateLimitCounter).where(
                    RateLimitCounter.bucket == bucket,
                    RateLimitCounter.window_start < window,
                )
            )
    except SQLAlchemyError:
        logger.exception("Rate limit check failed for %s; allowing the request.", bucket)
        return None

    if hits > limit:
        return int((window + timedelta(seconds=window_seconds) - now).total_seconds())
    return None


def limit(name: str, *, key=client_ip):
    """Apply the ``name`` limit from ``RATE_LIMITS`` to a view.

    ``key`` decides who is being counted — the caller's address by default,
    but an endpoint that already knows the account can pass something more
    precise.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            rule = current_app.config["RATE_LIMITS"].get(name)
            if rule is None:
                return view(*args, **kwargs)

            allowance, window_seconds = rule
            bucket = f"{name}:{key()}"
            retry_after = hit(bucket, allowance, window_seconds)
            if retry_after is not None:
                # The bucket names the limit and the address, which is what
                # an operator needs to tell one noisy client from an attack.
                logger.warning("Rate limit exceeded: %s (%s)", bucket, request.path)
                raise RateLimitError(
                    "Too many requests. Please wait a moment and try again.",
                )
            return view(*args, **kwargs)

        return wrapper

    return decorator


def reset(bucket_prefix: str) -> None:
    """Forget everything counted under ``bucket_prefix``.

    Used after a successful sign-in, so someone who fumbled their password a
    few times is not still carrying those attempts around.
    """
    try:
        with db.engine.begin() as connection:
            connection.execute(
                delete(RateLimitCounter).where(
                    RateLimitCounter.bucket.startswith(bucket_prefix)
                )
            )
    except SQLAlchemyError:
        logger.exception("Could not clear rate limit counters for %s.", bucket_prefix)


def current_hits(bucket: str, window_seconds: int) -> int:
    """How many hits ``bucket`` has in the open window. For tests."""
    window = _window_start(datetime.now(timezone.utc), window_seconds)
    with db.engine.begin() as connection:
        found = connection.execute(
            select(RateLimitCounter.hits).where(
                RateLimitCounter.bucket == bucket,
                RateLimitCounter.window_start == window,
            )
        ).scalar()
    return found or 0
