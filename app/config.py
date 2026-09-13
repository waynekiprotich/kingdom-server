"""Configuration loaded from the environment (spec §2, §28).

Nothing in here has a usable production default. Secrets come from the
environment or the app refuses to start.
"""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit


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


def _require_strict_origins(origins: list[str]) -> list[str]:
    """Production CORS may name exact HTTPS origins and nothing else.

    A wildcard would let any site read authenticated API responses, and
    Flask-CORS treats an entry containing regex characters as a pattern — so
    ``*`` is not the only way to accidentally allow everyone. Plain ``http``
    would let a network attacker's page pose as the storefront.
    """
    for origin in origins:
        parts = urlsplit(origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.path not in ("",)
            or parts.query
            or parts.fragment
            or any(character in origin for character in "*?[](){}^$\\|+")
        ):
            raise ConfigError(
                f"Refusing to start: CORS_ORIGINS entry {origin!r} is not an exact "
                "https:// origin (scheme and host only, no trailing slash, no wildcard)."
            )
    return origins


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

    #: Largest request body accepted, in bytes. Every endpoint takes a small
    #: JSON document — product photographs go straight from the admin's
    #: browser to Cloudinary and never pass through here — so anything near
    #: this size is either a mistake or an attempt to tie up a worker. Werkzeug
    #: answers 413 before the body is read.
    MAX_CONTENT_LENGTH = 1024 * 1024

    # SQLAlchemy
    SQLALCHEMY_DATABASE_URI: str = ""
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    #: Connection pooling. The defaults (5 + 10 overflow, no recycle) are sized
    #: for one process talking to a database on the same machine; neither is
    #: true in production.
    #:
    #: ``pool_recycle`` is the important one. A managed PostgreSQL — and every
    #: NAT and load balancer between here and it — drops connections that have
    #: been idle for a few minutes. Without a recycle, the pool keeps handing
    #: those out and ``pool_pre_ping`` discovers they are dead one round trip
    #: at a time, on the first request after every quiet spell. Recycling below
    #: that threshold means the pool retires them before anyone waits on one.
    #:
    #: The size is per *worker*, and gunicorn runs several, so the ceiling that
    #: matters is workers × (pool_size + max_overflow). Kept deliberately small:
    #: a small instance's connection limit is the scarce resource, not
    #: connections themselves, and the rate limiter checks out a second
    #: connection of its own on every protected request (see
    #: services/rate_limit.py) — so the real demand is higher than the request
    #: concurrency suggests.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
        "pool_size": 5,
        "max_overflow": 5,
        # Without a timeout a worker blocks indefinitely on an unreachable
        # database, and the platform's health check is what eventually notices.
        # Five seconds turns that into a 500 with a request id attached.
        "connect_args": {
            "connect_timeout": 5,
            # Shows up in pg_stat_activity, so "what is holding this
            # connection?" has an answer that names the app.
            "application_name": "kingdom-api",
        },
    }

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

    #: How many proxies sit in front of the app. Render puts exactly one there;
    #: running locally there are none. ProxyFix trusts this many hops when it
    #: rewrites remote_addr, and trusting more than actually exist would let a
    #: caller forge their own address through X-Forwarded-For — which is the
    #: whole basis of the rate limiting below.
    TRUSTED_PROXY_HOPS = 0

    #: name -> (requests allowed, window in seconds). Per client address
    #: unless the endpoint says otherwise. Deliberately generous: these are
    #: here to stop scripted abuse, not to inconvenience a customer who taps
    #: "place order" twice because the first tap seemed slow.
    RATE_LIMITS = {
        # Per address, shared by admin and customer sign-in, and never reset
        # by a successful sign-in: resetting let anyone with a throwaway
        # account clear their own counter between guesses at someone else's.
        # Sized for CGNAT (see "orders"). Targeted guessing at one account is
        # stopped by the per-account lockout, not by this.
        "login": (30, 900),
        "refresh": (60, 900),
        # Orders are counted per address, and in Kenya an address is not a
        # person: Safaricom and the other carriers put large numbers of mobile
        # customers behind one public IP (CGNAT), so everyone shopping over
        # mobile data from one carrier gateway shares this allowance. Sized
        # for that, not for one shopper — the limit still stops a script
        # hammering checkout, but a busy evening will not start turning real
        # customers away. Lower it only with the shared-IP case in mind.
        "orders": (40, 600),
        # An STK push makes a stranger's phone buzz, so this one is abuse
        # protection in a way the others are not: the limit is what stops a
        # script using the shop to harass a number. Sized for CGNAT like
        # "orders" above, and backed by a per-order guard in
        # services/payments.py that refuses a second push while one is still
        # outstanding — that guard, not this limit, is what stops a customer
        # tapping "Pay" three times from getting three prompts.
        "payments": (30, 600),
        # Sign-up is cheap to attempt and creates a row, so it is limited even
        # though it is not sensitive in the way login is. Sized for CGNAT like
        # the others — a shared carrier gateway must not lock out a street.
        "register": (15, 3600),
        # Claiming an order is a token guess if you are not the owner. Tight,
        # because a legitimate customer does this a handful of times ever.
        "claim": (10, 600),
        "upload-signature": (60, 3600),
    }

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

    #: How the STK push is delivered.
    #:
    #: ``daraja``    — the real thing. Talks to Safaricom.
    #: ``simulator`` — no Daraja account needed. The push is not sent, but a
    #:                 synthetic callback is fed through the *real* callback
    #:                 handler a few seconds later, so the Payment row, the
    #:                 amount check, the idempotency constraint and the order
    #:                 transition are all genuinely exercised. Refused outright
    #:                 in production (see ProductionConfig) — a mode that can
    #:                 mark orders paid without Safaricom's involvement must
    #:                 never be reachable on a live shop.
    MPESA_MODE = "simulator"

    #: Seconds the simulator waits before delivering its callback, so the
    #: storefront's "check your phone" state is actually visible.
    MPESA_SIMULATOR_DELAY_SECONDS = 4.0

    #: Whether the simulator delivers that callback by itself, on a timer.
    #: True is what makes the demo work with no manual step. Tests turn it off
    #: and post the callback themselves: a background thread firing seconds
    #: later, against a database the test has already torn down, is how a suite
    #: becomes intermittently red for reasons nobody can reproduce.
    MPESA_SIMULATOR_AUTO_CALLBACK = True

    #: Safaricom's sandbox host and its published test shortcode and passkey.
    #: These are public — they are printed in Daraja's own documentation and
    #: are the same for every developer — so they are defaults, not secrets.
    #: The consumer key and secret are per-account and are not, which is why
    #: they stay in the environment.
    MPESA_BASE_URL = "https://sandbox.safaricom.co.ke"
    MPESA_SHORTCODE_DEFAULT = "174379"
    MPESA_PASSKEY_DEFAULT = (
        "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
    )

    #: ``CustomerPayBillOnline`` for a paybill, ``CustomerBuyGoodsOnline`` for
    #: a till. The sandbox shortcode above is a paybill.
    MPESA_TRANSACTION_TYPE = "CustomerPayBillOnline"

    #: Seconds to wait on Daraja. Deliberately short: a customer is watching a
    #: spinner, and Safaricom timing out is not a reason to hold a worker.
    MPESA_TIMEOUT_SECONDS = 15

    #: The shop's WhatsApp number in international form, digits only
    #: (``2547XXXXXXXX``). Empty hides the WhatsApp option entirely rather than
    #: rendering a link that goes nowhere.
    WHATSAPP_NUMBER = ""

    #: Widths generated for responsive product imagery (spec §17, §22).
    IMAGE_WIDTHS = (400, 800, 1200)

    #: How long `/api/products` may reuse a computed facet set, in seconds.
    #:
    #: Computing facets means scanning every variant in scope — 35 ms against a
    #: 60,000-variant catalog, on every request, including the homepage's
    #: featured strip which renders no filter panel at all.
    #:
    #: Sixty seconds because that is already how long the same response is
    #: cacheable for (`PUBLIC_CACHE_CONTROL`), so this introduces no staleness
    #: the API did not already have: a browser may serve a minute-old catalog
    #: page from its own cache regardless. An admin's newly added size still
    #: reaches the filter panel within the minute.
    #:
    #: Zero disables the cache and computes fresh every time.
    FACET_CACHE_SECONDS = 60

    #: Flat delivery fee charged on every order (spec §7). Not a secret and
    #: not per-environment, so it is a plain constant rather than something
    #: read from the environment — change it here when a real logistics
    #: partner and rate are settled.
    DELIVERY_FEE = Decimal("300.00")

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

        # The published sandbox pair stands in only while they are unset, so a
        # real shortcode and passkey always win.
        self.MPESA_SHORTCODE = self.MPESA_SHORTCODE or self.MPESA_SHORTCODE_DEFAULT
        self.MPESA_PASSKEY = self.MPESA_PASSKEY or self.MPESA_PASSKEY_DEFAULT

        self.MPESA_MODE = os.environ.get("MPESA_MODE", self.MPESA_MODE).strip().lower()
        if self.MPESA_MODE not in ("daraja", "simulator"):
            raise ConfigError(
                f"Unknown MPESA_MODE {self.MPESA_MODE!r}. Expected 'daraja' or "
                "'simulator'."
            )

        self.MPESA_BASE_URL = os.environ.get(
            "MPESA_BASE_URL", self.MPESA_BASE_URL
        ).rstrip("/")
        self.MPESA_TRANSACTION_TYPE = os.environ.get(
            "MPESA_TRANSACTION_TYPE", self.MPESA_TRANSACTION_TYPE
        )

        # Digits only: wa.me rejects '+' and spaces, and a number that silently
        # does not open a chat is worse than no button at all.
        self.WHATSAPP_NUMBER = "".join(
            character
            for character in os.environ.get("WHATSAPP_NUMBER", "")
            if character.isdigit()
        )


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True

    def __init__(self) -> None:
        super().__init__()
        # FLASK_ENV defaults to development, so a host that forgot to set it
        # would quietly run this config: the M-Pesa simulator allowed, seed
        # data allowed, debug on. Render marks every service with RENDER, so
        # the one hosting platform in use can be told apart from a laptop.
        if os.environ.get("RENDER"):
            raise ConfigError(
                "Refusing to start the development config on Render. Set "
                "FLASK_ENV=production in the service's environment."
            )
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

    #: Off. The facet cache lives in a module-level dict, which outlives the
    #: per-test application and its database — a test that seeds a catalog and
    #: then asserts on the sizes it offers would otherwise be reading the
    #: previous test's answer.
    FACET_CACHE_SECONDS = 0

    #: Tests drive the callback themselves; see the note on the base class.
    MPESA_SIMULATOR_AUTO_CALLBACK = False

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

    #: Never the simulator. See __init__ — this is enforced, not just defaulted.
    MPESA_MODE = "daraja"

    #: Render terminates TLS and forwards to the app, so there is one hop.
    #: Override with TRUSTED_PROXY_HOPS if that ever stops being true.
    TRUSTED_PROXY_HOPS = 1

    def __init__(self) -> None:
        super().__init__()
        self.TRUSTED_PROXY_HOPS = int(
            os.environ.get("TRUSTED_PROXY_HOPS", self.TRUSTED_PROXY_HOPS)
        )
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
        if self.SECRET_KEY == self.JWT_SECRET_KEY:
            # One leaked value should not hand over both signing keys.
            raise ConfigError(
                "Refusing to start: SECRET_KEY and JWT_SECRET_KEY must be different values."
            )
        self.SQLALCHEMY_DATABASE_URI = _normalise_database_url(
            os.environ["DATABASE_URL"]
        )
        self.CORS_ORIGINS = _require_strict_origins(
            _split_origins(os.environ["CORS_ORIGINS"])
        )
        if not self.CORS_ORIGINS:
            raise ConfigError("Refusing to start: CORS_ORIGINS names no origins.")

        # The simulator marks orders paid without Safaricom ever being asked.
        # That is exactly what it is for in development, and exactly what must
        # not be reachable on a shop taking real money — so this refuses to
        # start rather than quietly falling back, which would leave a live site
        # accepting free orders with nothing in the logs to say so.
        if self.MPESA_MODE != "daraja":
            raise ConfigError(
                "Refusing to start: MPESA_MODE must be 'daraja' in production. "
                "The simulator confirms payments without Safaricom and would "
                "let anyone mark an order paid."
            )


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
