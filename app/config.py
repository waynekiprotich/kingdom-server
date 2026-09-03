"""Configuration loaded from the environment (spec §2, §28).

Nothing in here has a usable production default. Secrets come from the
environment or the app refuses to start.
"""

from __future__ import annotations

import os
from datetime import timedelta


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _normalise_database_url(url: str) -> str:
    """Force the psycopg 3 driver.

    Managed hosts hand out ``postgres://`` URLs, which SQLAlchemy 2 rejects,
    and a bare ``postgresql://`` resolves to psycopg2, which is not installed.
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _split_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


#: HMAC-SHA256 wants at least this much key material (RFC 7518 §3.2). A short
#: secret is a weak secret, so production refuses to start with one.
MIN_SECRET_BYTES = 32


def _require_strong(name: str, value: str) -> str:
    if len(value.encode()) < MIN_SECRET_BYTES:
        raise ConfigError(
            f"Refusing to start: {name} must be at least {MIN_SECRET_BYTES} bytes. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    return value


class BaseConfig:
    """Settings shared by every environment."""

    #: Which environment this is. Guards that must not fire in production read
    #: this rather than DEBUG — the Flask CLI overwrites DEBUG from FLASK_DEBUG
    #: after the config is loaded, so DEBUG is not trustworthy in a command.
    ENV_NAME = "base"

    # Flask
    SECRET_KEY: str = ""
    JSON_SORT_KEYS = False

    # SQLAlchemy
    SQLALCHEMY_DATABASE_URI: str = ""
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # JWT (spec §15) — admin sessions only; customers never authenticate.
    JWT_SECRET_KEY: str = ""
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=7)
    JWT_ERROR_MESSAGE_KEY = "message"

    # CORS (spec §16)
    CORS_ORIGINS: list[str] = []

    # Admin brute-force protection (spec §15)
    MAX_FAILED_LOGINS = 5
    LOGIN_LOCKOUT_MINUTES = 15

    # External services. Read in __init__, not in the class body: the class
    # body runs at import time, which is before create_app() calls
    # load_dotenv(), so class-level os.environ reads would always be empty.
    MPESA_CONSUMER_KEY = ""
    MPESA_CONSUMER_SECRET = ""
    MPESA_PASSKEY = ""
    MPESA_SHORTCODE = ""
    MPESA_CALLBACK_URL = ""
    CLOUDINARY_CLOUD_NAME = ""
    CLOUDINARY_API_KEY = ""
    CLOUDINARY_API_SECRET = ""

    #: Widths generated for responsive product imagery (spec §17, §22).
    IMAGE_WIDTHS = (400, 800, 1200)

    def __init__(self) -> None:
        for name in (
            "MPESA_CONSUMER_KEY",
            "MPESA_CONSUMER_SECRET",
            "MPESA_PASSKEY",
            "MPESA_SHORTCODE",
            "MPESA_CALLBACK_URL",
            "CLOUDINARY_CLOUD_NAME",
            "CLOUDINARY_API_KEY",
            "CLOUDINARY_API_SECRET",
        ):
            setattr(self, name, os.environ.get(name, ""))


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True

    def __init__(self) -> None:
        super().__init__()
        self.SECRET_KEY = os.environ.get(
            "SECRET_KEY", "development-only-flask-secret-key-not-for-production"
        )
        self.JWT_SECRET_KEY = os.environ.get(
            "JWT_SECRET_KEY", "development-only-jwt-signing-key-not-for-production"
        )
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise ConfigError(
                "DATABASE_URL is not set. Copy .env.example to .env and point it "
                "at your local PostgreSQL database."
            )
        self.SQLALCHEMY_DATABASE_URI = _normalise_database_url(database_url)
        self.CORS_ORIGINS = _split_origins(
            os.environ.get("CORS_ORIGINS", "http://localhost:5173")
        )


class TestingConfig(BaseConfig):
    """Test settings.

    Runs against ``TEST_DATABASE_URL`` when one is set, and falls back to
    in-memory SQLite when it is not. Prefer the real thing: constraints,
    ``ondelete`` rules and ``Numeric`` precision are exactly what these tests
    are asserting, and SQLite only approximates them.
    """

    ENV_NAME = "testing"
    TESTING = True
    SECRET_KEY = "testing-secret-key-long-enough-for-hmac-sha256"
    JWT_SECRET_KEY = "testing-jwt-signing-key-long-enough-for-hmac"
    CORS_ORIGINS = ["http://localhost:5173"]

    def __init__(self) -> None:
        super().__init__()
        test_url = os.environ.get("TEST_DATABASE_URL")
        if test_url:
            self.SQLALCHEMY_DATABASE_URI = _normalise_database_url(test_url)
        else:
            self.SQLALCHEMY_DATABASE_URI = "sqlite+pysqlite:///:memory:"
            self.SQLALCHEMY_ENGINE_OPTIONS = {}


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    DEBUG = False

    def __init__(self) -> None:
        super().__init__()
        missing = [
            name
            for name in ("SECRET_KEY", "JWT_SECRET_KEY", "DATABASE_URL", "CORS_ORIGINS")
            if not os.environ.get(name)
        ]
        if missing:
            raise ConfigError(
                "Refusing to start: missing required environment variables: "
                + ", ".join(missing)
            )

        self.SECRET_KEY = _require_strong("SECRET_KEY", os.environ["SECRET_KEY"])
        self.JWT_SECRET_KEY = _require_strong(
            "JWT_SECRET_KEY", os.environ["JWT_SECRET_KEY"]
        )
        self.SQLALCHEMY_DATABASE_URI = _normalise_database_url(
            os.environ["DATABASE_URL"]
        )
        self.CORS_ORIGINS = _split_origins(os.environ["CORS_ORIGINS"])


_CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None):
    """Return a config object for ``name`` (defaults to $FLASK_ENV)."""
    resolved = name or os.environ.get("FLASK_ENV", "development")
    try:
        config_class = _CONFIGS[resolved]
    except KeyError:
        raise ConfigError(
            f"Unknown environment {resolved!r}. Expected one of: "
            + ", ".join(_CONFIGS)
        ) from None
    return config_class()
