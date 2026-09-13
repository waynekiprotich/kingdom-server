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
import re
import time
from typing import Any

from flask import current_app

BASE_URL = "https://res.cloudinary.com"

#: Applied to every delivery URL: modern format and quality chosen per browser.
DEFAULT_TRANSFORMS = ("f_auto", "q_auto")

#: Product photography is shot portrait; cards and galleries share the ratio so
#: a grid never jumps as images load.
PRODUCT_RATIO = "3:4"

#: Folders an admin may upload into. The signature covers the folder, so a
#: client cannot swap it — but it chooses what it asks to have signed, and
#: this is the list of answers.
UPLOAD_FOLDERS = ("products",)

#: Photographs only. Cloudinary refuses anything else for a signed upload
#: that names these, which rules out SVG (script-capable) and PDFs as well as
#: anything that is not an image at all. Signed, so the browser cannot drop it.
ALLOWED_FORMATS = ("avif", "heic", "jpeg", "jpg", "png", "webp")

#: Incoming transformation applied as the file is stored: nothing is kept
#: larger than twice the widest size the storefront ever requests
#: (``IMAGE_WIDTHS``), so a 50-megapixel phone photo does not become the
#: original every derived size is computed from. ``c_limit`` only ever
#: shrinks. Signed along with everything else.
INCOMING_TRANSFORMATION = "c_limit,w_2400,h_2400"

#: Largest file the dashboard will send, in bytes. Enforced in the browser,
#: which is advisory — Cloudinary's own plan limit is the hard ceiling — but it
#: saves an admin on mobile data from uploading something that will be
#: shrunk anyway. Returned with the signature so the two cannot disagree.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: What a stored public_id may look like: folder segments of plain characters.
#: It is interpolated into delivery URLs after the transformation segment, so
#: a comma or a slash-led segment such as ``w_5000`` would otherwise be read
#: by Cloudinary as a transformation of the caller's choosing.
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)*$")


def is_valid_public_id(public_id: str) -> bool:
    if len(public_id) > 255 or ".." in public_id:
        return False
    if not _PUBLIC_ID.match(public_id):
        return False
    # A first segment shaped like a transformation (w_400, c_fill, ar_3:4...)
    # would be parsed as one.
    return not re.match(r"^[a-z]{1,3}_", public_id.split("/", 1)[0])


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
    return name.strip()


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

    if folder not in UPLOAD_FOLDERS:
        raise ValueError(f"Uploads are not allowed into folder {folder!r}.")

    to_sign = {
        "allowed_formats": ",".join(ALLOWED_FORMATS),
        "folder": folder,
        "timestamp": int(time.time()),
        "transformation": INCOMING_TRANSFORMATION,
    }

    return {
        "cloud_name": cloud_name,
        "api_key": api_key,
        "upload_url": f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
        "signature": sign(to_sign, api_secret),
        "max_bytes": MAX_UPLOAD_BYTES,
        **to_sign,
    }
