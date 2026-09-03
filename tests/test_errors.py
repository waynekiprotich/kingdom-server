"""The error contract from spec §14 — every failure has the same shape."""


def assert_envelope(response, code: str, status: int):
    assert response.status_code == status
    body = response.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    # Never anything else at the top level — no stack traces, no internals.
    assert set(body) == {"success", "error"}


def test_unknown_route_uses_the_envelope(client):
    assert_envelope(client.get("/api/does-not-exist"), "NOT_FOUND", 404)


def test_wrong_method_uses_the_envelope(client):
    assert_envelope(client.get("/api/admin/auth/login"), "METHOD_NOT_ALLOWED", 405)


def test_missing_body_is_a_validation_error(client):
    assert_envelope(client.post("/api/admin/auth/login"), "VALIDATION_ERROR", 422)


def test_missing_field_is_a_validation_error(client):
    response = client.post("/api/admin/auth/login", json={"email": "a@b.com"})
    assert_envelope(response, "VALIDATION_ERROR", 422)
    assert "password" in response.get_json()["error"]["message"]
