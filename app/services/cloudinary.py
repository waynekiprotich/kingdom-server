"""Cloudinary image delivery and signed uploads (spec §17).

Two jobs, and only two, which is why there is no SDK here:

1. Turn a stored ``public_id`` into delivery URLs at the sizes the storefront
   asks for. That is string formatting.
2. Sign an upload request so the admin dashboard can send the file straight to
   Cloudinary. That is one SHA-256 digest.

The API secret is used to produce a signature and is never returned, logged, or
sent to a browser. The file bytes never pass through this server — the admin
client uploads directly, using a signature minted here.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from flask import current_app

BASE_URL = "https://res.cloudinary.com"

#: Applied to every delivery URL: modern format and quality chosen per browser.
DEFAULT_TRANSFORMS = ("f_auto", "q_auto")

#: Product photography is shot portrait; cards and galleries share the ratio so
#: a grid never jumps as images load.
PRODUCT_RATIO = "3:4"


class CloudinaryNotConfigured(RuntimeError):
    """Raised when an operation needs credentials that are not present."""


def is_configured() -> bool:
    return bool(current_app.config.get("CLOUDINARY_CLOUD_NAME"))


def _cloud_name() -> str:
    name = current_app.config.get("CLOUDINARY_CLOUD_NAME")
    if not name:
        raise CloudinaryNotConfigured(
            "CLOUDINARY_CLOUD_NAME is not set. Add it to server/.env."
        )
    return name


def build_url(
    public_id: str,
    *,
    width: int | None = None,
    ratio: str | None = PRODUCT_RATIO,
    crop: str = "fill",
) -> str:
    """Delivery URL for ``public_id`` at an optional width and aspect ratio."""
    transforms: list[str] = list(DEFAULT_TRANSFORMS)

    if width is not None:
        transforms.append(f"w_{width}")
    if ratio is not None:
        transforms.extend([f"ar_{ratio}", f"c_{crop}"])

    return f"{BASE_URL}/{_cloud_name()}/image/upload/{','.join(transforms)}/{public_id}"


def build_srcset(public_id: str, widths: tuple[int, ...], **kwargs: Any) -> str:
    """A ``srcset`` string so the browser picks the width it actually needs."""
    return ", ".join(
        f"{build_url(public_id, width=width, **kwargs)} {width}w" for width in widths
    )


def sign(params: dict[str, Any], api_secret: str) -> str:
    """Cloudinary's upload signature.

    Parameters are sorted, joined as ``k=v`` pairs with ``&``, the secret is
    appended, and the result is hashed. Empty values are excluded, as are the
    parameters Cloudinary never signs.
    """
    unsigned = {"file", "cloud_name", "resource_type", "api_key"}
    payload = "&".join(
        f"{key}={params[key]}"
        for key in sorted(params)
        if key not in unsigned and params[key] not in (None, "")
    )
    return hashlib.sha256(f"{payload}{api_secret}".encode()).hexdigest()


def signed_upload_params(folder: str = "products") -> dict[str, Any]:
    """Everything an admin client needs to upload one image directly.

    The secret stays here; only the signature it produces goes out.
    """
    cloud_name = _cloud_name()
    api_key = current_app.config.get("CLOUDINARY_API_KEY")
    api_secret = current_app.config.get("CLOUDINARY_API_SECRET")

    if not api_key or not api_secret:
        raise CloudinaryNotConfigured(
            "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET are required to upload."
        )

    to_sign = {"folder": folder, "timestamp": int(time.time())}

    return {
        "cloud_name": cloud_name,
        "api_key": api_key,
        "upload_url": f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
        "signature": sign(to_sign, api_secret),
        **to_sign,
    }
