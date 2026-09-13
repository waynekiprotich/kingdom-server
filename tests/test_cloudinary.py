"""Cloudinary URL building and upload signing (spec §17).

The signature algorithm is deterministic, so it is testable without an account.
"""

import hashlib

import pytest

from app.extensions import db
from app.services import cloudinary


@pytest.fixture
def configured(app):
    app.config["CLOUDINARY_CLOUD_NAME"] = "kingdom"
    app.config["CLOUDINARY_API_KEY"] = "123456789"
    app.config["CLOUDINARY_API_SECRET"] = "top-secret"
    return app


def test_url_carries_format_and_quality_hints(configured):
    url = cloudinary.build_url("products/shirt", width=800)

    assert url == (
        "https://res.cloudinary.com/kingdom/image/upload/"
        "f_auto,q_auto,w_800,ar_3:4,c_fill/products/shirt"
    )


def test_srcset_offers_every_width(configured):
    srcset = cloudinary.build_srcset("products/shirt", (400, 800))

    assert srcset.count("https://") == 2
    assert srcset.endswith("800w")


def test_signature_matches_cloudinarys_algorithm(configured):
    """Sorted k=v pairs joined by &, secret appended, SHA-256."""
    params = {"timestamp": 1700000000, "folder": "products"}

    expected = hashlib.sha256(
        b"folder=products&timestamp=1700000000top-secret"
    ).hexdigest()

    assert cloudinary.sign(params, "top-secret") == expected


def test_signature_excludes_the_parameters_cloudinary_never_signs(configured):
    with_noise = {
        "timestamp": 1700000000,
        "folder": "products",
        "api_key": "123456789",
        "cloud_name": "kingdom",
        "file": "binary",
    }
    clean = {"timestamp": 1700000000, "folder": "products"}

    assert cloudinary.sign(with_noise, "s") == cloudinary.sign(clean, "s")


def test_upload_params_never_include_the_secret(configured):
    params = cloudinary.signed_upload_params()

    assert "top-secret" not in str(params)
    assert set(params) == {
        "cloud_name",
        "api_key",
        "upload_url",
        "signature",
        "max_bytes",
        "folder",
        "timestamp",
        # Signed restrictions on what may be uploaded (added in hardening).
        "allowed_formats",
        "transformation",
    }


def test_upload_signature_endpoint_requires_authentication(client):
    assert client.post("/api/admin/images/upload-signature").status_code == 401


def test_upload_signature_endpoint_returns_params_for_an_admin(
    client, configured, auth_headers
):
    response = client.post("/api/admin/images/upload-signature", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["cloud_name"] == "kingdom"
    assert data["signature"]
    assert "api_secret" not in data


def test_a_deactivated_admin_cannot_mint_an_upload_signature(
    client, configured, admin, auth_headers
):
    """Signing an upload is an admin action, so it follows the same rule as
    the rest of the admin API: deactivating an account cuts it off now, not
    whenever its access token happens to expire.
    """
    admin.is_active = False
    db.session.commit()

    response = client.post("/api/admin/images/upload-signature", headers=auth_headers)

    assert response.status_code == 403


def test_upload_signature_fails_cleanly_when_not_configured(client, app, auth_headers):
    app.config["CLOUDINARY_CLOUD_NAME"] = ""

    response = client.post("/api/admin/images/upload-signature", headers=auth_headers)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "IMAGE_SERVICE_UNAVAILABLE"
    # The operator's reason stays in the logs, not the response.
    assert "CLOUDINARY" not in response.get_json()["error"]["message"]
