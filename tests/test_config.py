"""Configuration guardrails (spec §16, §28)."""

import pytest

from app.config import ConfigError, _normalise_database_url, get_config

STRONG = "x" * 48


def test_production_refuses_to_start_without_secrets(monkeypatch):
    for name in ("SECRET_KEY", "JWT_SECRET_KEY", "DATABASE_URL", "CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigError) as excinfo:
        get_config("production")

    assert "SECRET_KEY" in str(excinfo.value)


def test_production_rejects_a_short_signing_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", STRONG)
    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/kingdom")
    monkeypatch.setenv("CORS_ORIGINS", "https://example.com")

    with pytest.raises(ConfigError) as excinfo:
        get_config("production")

    assert "JWT_SECRET_KEY" in str(excinfo.value)


def test_production_accepts_strong_secrets(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", STRONG)
    monkeypatch.setenv("JWT_SECRET_KEY", STRONG)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/kingdom")
    monkeypatch.setenv("CORS_ORIGINS", "https://example.com, https://www.example.com")
    # Set explicitly rather than left to the default: a developer's .env has
    # MPESA_MODE=simulator in it, load_dotenv puts that in the environment, and
    # production refuses to start on it. Naming it here keeps the test
    # deterministic and states what a valid production environment contains.
    monkeypatch.setenv("MPESA_MODE", "daraja")

    config = get_config("production")

    assert config.CORS_ORIGINS == ["https://example.com", "https://www.example.com"]
    assert config.SQLALCHEMY_DATABASE_URI.startswith("postgresql+psycopg://")


@pytest.mark.parametrize(
    "given",
    [
        "postgres://user:pw@host/db",
        "postgresql://user:pw@host/db",
        "postgresql+psycopg://user:pw@host/db",
    ],
)
def test_database_urls_all_resolve_to_psycopg3(given):
    """Managed hosts hand out postgres:// URLs that SQLAlchemy 2 rejects."""
    assert _normalise_database_url(given) == "postgresql+psycopg://user:pw@host/db"


def test_unknown_environment_is_rejected():
    with pytest.raises(ConfigError):
        get_config("staging-typo")
