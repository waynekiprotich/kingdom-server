"""Admin image uploads (spec §17).

The browser uploads the file straight to Cloudinary using a signature minted
here. Two consequences worth stating: image bytes never occupy this server's
memory or bandwidth, and the API secret never leaves it.
"""

from __future__ import annotations

from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from app.errors import ApiError
from app.services import cloudinary
from app.validation import query_str

bp = Blueprint("admin_media", __name__, url_prefix="/api/admin/images")


class ImageServiceUnavailable(ApiError):
    status_code = 503
    code = "IMAGE_SERVICE_UNAVAILABLE"
    message = "Image uploads are not configured on this server."


@bp.post("/upload-signature")
@jwt_required()
def upload_signature():
    folder = query_str("folder", max_length=60) or "products"

    try:
        params = cloudinary.signed_upload_params(folder=folder)
    except cloudinary.CloudinaryNotConfigured as error:
        # The reason is useful to an operator reading logs, not to a client.
        raise ImageServiceUnavailable() from error

    return jsonify({"success": True, "data": params})
