"""Small shared helpers."""

from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9]+")

#: A Kenyan mobile subscriber number: 9 digits, starting 7 (Safaricom/Airtel/
#: Telkom GSM ranges) or 1 (Safaricom's newer 01xxxxxxxx range).
_KENYAN_SUBSCRIBER = re.compile(r"^[71]\d{8}$")


def slugify(value: str, *, max_length: int = 200) -> str:
    """A URL-safe slug.

    Accents are folded rather than dropped so "Kitenge Écru" becomes
    "kitenge-ecru" instead of "kitenge-cru".
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = _NON_WORD.sub("-", ascii_only.lower()).strip("-")
    return slug[:max_length].strip("-")


def normalise_kenyan_phone(raw: str) -> str | None:
    """Fold every common way a customer types their number into ``2547XXXXXXXX``.

    Accepts ``0712345678``, ``712345678``, ``+254712345678`` and
    ``254712345678`` (and the ``01...`` equivalents), because a checkout form
    is exactly the wrong place to make someone guess the format we want.
    Returns ``None`` if what's left after stripping punctuation isn't a
    plausible Kenyan mobile number — this is the only signal the M-Pesa STK
    push will have later, so it is worth getting firmly right now.
    """
    digits = re.sub(r"[^\d]", "", raw)

    if digits.startswith("254"):
        subscriber = digits[3:]
    elif digits.startswith("0"):
        subscriber = digits[1:]
    else:
        subscriber = digits

    if not _KENYAN_SUBSCRIBER.match(subscriber):
        return None

    return f"254{subscriber}"


def generate_order_number(order_id: int) -> str:
    """A human-friendly order number derived from the primary key.

    Deterministic and collision-free by construction — no separate counter
    table to keep in sync, no race between two checkouts picking the same
    number. ``order_id`` must come from a row that has already been flushed
    (so it has been assigned by the database), and this is not called until
    after that.
    """
    return f"KC-{100_000 + order_id}"
