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
from app.security import register_security_headers

# Import for the side effect of registering every table on Base.metadata.
from app import models  # noqa: F401

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def create_app(config_name: str | None = None) -> Flask:
    load_dotenv()

    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Behind a proxy, remote_addr is the proxy. Rate limiting and audit logs
    # both key off the caller's address, so it has to be the real one — and
    # only as many hops as actually exist may be trusted, or the header can
    # simply be forged.
    hops = app.config["TRUSTED_PROXY_HOPS"]
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    register_security_headers(app)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(MIGRATIONS_DIR))
    jwt.init_app(app)
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        supports_credentials=False,
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
