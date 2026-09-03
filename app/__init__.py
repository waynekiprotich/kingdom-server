"""Application factory (spec §3).

Nothing at import time depends on a running database, so ``create_app`` is safe
to call from tests, the CLI and a WSGI server alike.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import get_config
from app.errors import register_error_handlers
from app.extensions import cors, db, jwt, migrate
from app.jwt_callbacks import register_jwt_callbacks
from app.performance import register_performance
from app.request_id import RequestIdFilter, register_request_id
from app.security import register_security_headers

# Import for the side effect of registering every table on Base.metadata.
from app import models  # noqa: F401

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def create_app(config_name: str | None = None) -> Flask:
    load_dotenv()

    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    # `request_id` comes from RequestIdFilter below. Having it in the format
    # is the whole point: a traceback that cannot be tied to the request that
    # caused it is a traceback nobody can act on.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdFilter())

    # Behind a proxy, remote_addr is the proxy. Rate limiting and audit logs
    # both key off the caller's address, so it has to be the real one — and
    # only as many hops as actually exist may be trusted, or the header can
    # simply be forged.
    hops = app.config["TRUSTED_PROXY_HOPS"]
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    # First, so everything registered after it can log against an id.
    register_request_id(app)
    register_security_headers(app)
    register_performance(app)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(MIGRATIONS_DIR))
    jwt.init_app(app)
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=False,
        # Catalog reads are deliberately "simple" requests and are never
        # preflighted (see `services/api.js`). What remains — an admin's
        # Authorization header, a checkout's JSON body — cannot avoid a
        # preflight, so let the browser remember the answer instead of
        # spending a round trip on it before every single request.
        max_age=86400,
    )

    register_error_handlers(app)
    register_jwt_callbacks(jwt)

    from app.blueprints import (
        admin_auth,
        admin_catalog,
        admin_media,
        admin_orders,
        catalog,
        checkout,
        health,
    )

    app.register_blueprint(health.bp)
    app.register_blueprint(admin_auth.bp)
    app.register_blueprint(admin_media.bp)
    app.register_blueprint(admin_catalog.bp)
    app.register_blueprint(admin_orders.bp)
    app.register_blueprint(catalog.bp)
    app.register_blueprint(checkout.bp)

    from app.cli import register_cli

    register_cli(app)

    return app
