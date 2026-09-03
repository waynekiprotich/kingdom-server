"""Small shared helpers."""

from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, max_length: int = 200) -> str:
    """A URL-safe slug.

    Accents are folded rather than dropped so "Kitenge Écru" becomes
    "kitenge-ecru" instead of "kitenge-cru".
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = _NON_WORD.sub("-", ascii_only.lower()).strip("-")
    return slug[:max_length].strip("-")
