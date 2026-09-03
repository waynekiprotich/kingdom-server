"""Response hardening headers (spec §16).

This is a JSON API, not a website, which makes the useful set small and
strict: nothing here is meant to be framed, sniffed, or rendered as markup,
so the policy can simply say so rather than enumerate exceptions.

Headers are set with ``setdefault`` so a view that has already made a
deliberate choice — Flask-CORS writing its own, an endpoint opting into
caching — keeps it.
"""

from __future__ import annotations

from flask import Flask, request

#: Anything under these carries a person's details — an admin's view of the
#: business, or a guest's own order with their name, phone and address. The
#: public catalog is deliberately not here: it is the same for everyone and
#: the hot path, so it stays cacheable.
PRIVATE_PREFIXES = ("/api/admin", "/api/orders")


def register_security_headers(app: Flask) -> None:
    is_production = app.config.get("ENV_NAME") == "production"

    @app.after_request
    def set_security_headers(response):
        headers = response.headers

        # No browser should ever be guessing at the type of an API response,
        # nor rendering one as a document.
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")

        # The API serves JSON and nothing else — no scripts, styles, images or
        # frames of its own — so the tightest possible policy is also the
        # correct one.
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )

        if request.path.startswith(PRIVATE_PREFIXES):
            headers.setdefault("Cache-Control", "no-store")

        # Only meaningful over TLS, and only safe to promise once the deployed
        # site is actually HTTPS-only — which Render's domains are.
        if is_production:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        return response
