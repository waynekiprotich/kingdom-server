"""Transfer-level performance: compression and public cache validity.

Two things the API was leaving on the table, both measured rather than
assumed:

1. Every response went out uncompressed. A 24-product catalog page is ~20 kB
   of JSON and JSON compresses roughly five to one, so most of what we were
   sending was whitespace and repeated key names. On the mobile connections
   this shop is actually browsed over, that is the single largest win
   available and it costs one stdlib call.

2. Nothing on the public catalog carried a cache directive, so browsers fell
   back to heuristic caching — which, for a JSON response with no ``Last-
   Modified``, means none. Every back-navigation refetched the whole grid.

Compression is stdlib ``gzip`` rather than a package: it is one call, and the
project's rule is that a short helper beats a dependency. Brotli would
compress a further ~15% but is not in the standard library, and the gap does
not justify the install.
"""

from __future__ import annotations

import gzip

from flask import Flask, request

#: Public, identical for every visitor, and the hot path. These may sit in a
#: browser or CDN cache; nothing under them is specific to one person.
PUBLIC_CACHE_PREFIXES = ("/api/products", "/api/categories")

#: Fresh for a minute, then usable for five more while a replacement is
#: fetched in the background. A shopper paging back and forth gets an instant
#: render, and an admin's catalog edit is visible within the minute.
PUBLIC_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"

#: Below roughly a packet's worth there is nothing to win — the gzip header
#: alone is 18 bytes, and small JSON often ends up larger compressed.
MIN_COMPRESS_BYTES = 500

#: Only text compresses usefully. Images and PDFs are already compressed, and
#: running them through gzip burns CPU to add bytes.
COMPRESSIBLE_TYPES = (
    "application/json",
    "application/javascript",
    "text/",
    "+json",
    "+xml",
)


def _is_compressible(response) -> bool:
    if response.direct_passthrough:
        # A streamed or file-backed response has no body to read here.
        return False
    if response.status_code < 200 or response.status_code >= 300:
        return False
    if "Content-Encoding" in response.headers:
        return False
    if response.content_length is not None and response.content_length < MIN_COMPRESS_BYTES:
        return False

    mimetype = (response.mimetype or "").lower()
    return any(token in mimetype for token in COMPRESSIBLE_TYPES)


def register_performance(app: Flask) -> None:
    @app.after_request
    def compress_and_validate(response):
        is_public = request.path.startswith(PUBLIC_CACHE_PREFIXES)

        if is_public:
            response.headers.setdefault("Cache-Control", PUBLIC_CACHE_CONTROL)

            # An ETag turns a revalidation into a 304 with no body at all.
            # Computed on the uncompressed JSON, and before compression, so a
            # conditional hit costs nothing to serve.
            if not response.direct_passthrough and "ETag" not in response.headers:
                response.add_etag()
                response.make_conditional(request)

        if not _is_compressible(response):
            return response

        accepted = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accepted.lower():
            return response

        body = response.get_data()
        if len(body) < MIN_COMPRESS_BYTES:
            return response

        compressed = gzip.compress(body, compresslevel=6)
        if len(compressed) >= len(body):
            # Nothing gained; send the original rather than pay to decompress.
            return response

        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))

        # Without this a shared cache could hand a gzipped body to a client
        # that never asked for one.
        response.headers.add("Vary", "Accept-Encoding")

        return response
