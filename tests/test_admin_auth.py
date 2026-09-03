"""Admin authentication (spec §15)."""

from tests.conftest import TEST_PASSWORD


def test_login_returns_tokens_and_the_admin(client, admin):
    response = client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": TEST_PASSWORD},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["access_token"] and data["refresh_token"]
    assert data["admin"]["email"] == admin.email
    assert "password_hash" not in data["admin"]


def test_wrong_password_is_rejected(client, admin):
    response = client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "AUTHENTICATION_FAILED"


def test_unknown_email_looks_identical_to_a_wrong_password(client, admin):
    """No user enumeration: both paths give the same code and message."""
    unknown = client.post(
        "/api/admin/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-password"},
    )
    wrong = client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": "wrong-password"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.get_json() == wrong.get_json()


def test_account_locks_after_repeated_failures(client, app, admin):
    for _ in range(app.config["MAX_FAILED_LOGINS"]):
        client.post(
            "/api/admin/auth/login",
            json={"email": admin.email, "password": "wrong-password"},
        )

    # Even the correct password is refused while the lock holds.
    response = client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": TEST_PASSWORD},
    )

    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_successful_login_clears_earlier_failures(client, app, admin):
    client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": "wrong-password"},
    )
    client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": TEST_PASSWORD},
    )

    assert admin.failed_login_count == 0
    assert admin.last_login_at is not None


def test_me_requires_a_token(client):
    response = client.get("/api/admin/auth/me")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_me_returns_the_signed_in_admin(client, auth_headers, admin):
    response = client.get("/api/admin/auth/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.get_json()["data"]["admin"]["email"] == admin.email


def test_refresh_issues_a_new_access_token(client, tokens):
    response = client.post(
        "/api/admin/auth/refresh",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["access_token"]


def test_an_access_token_cannot_be_used_to_refresh(client, auth_headers):
    response = client.post("/api/admin/auth/refresh", headers=auth_headers)

    assert response.status_code == 401


def test_logout_revokes_the_token(client, auth_headers):
    assert client.get("/api/admin/auth/me", headers=auth_headers).status_code == 200

    logout = client.post("/api/admin/auth/logout", headers=auth_headers)
    assert logout.status_code == 200

    after = client.get("/api/admin/auth/me", headers=auth_headers)
    assert after.status_code == 401
    assert after.get_json()["error"]["code"] == "TOKEN_REVOKED"


def test_deactivated_admin_cannot_sign_in(client, app, admin):
    from app.extensions import db

    admin.is_active = False
    db.session.commit()

    response = client.post(
        "/api/admin/auth/login",
        json={"email": admin.email, "password": TEST_PASSWORD},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "FORBIDDEN"
