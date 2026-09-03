def test_health_reports_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "data": {"status": "ok"}}


def test_readiness_checks_the_database(client):
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "ready"
