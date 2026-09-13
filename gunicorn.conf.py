"""Gunicorn settings, picked up automatically from the working directory.

The start command stays ``gunicorn --bind 0.0.0.0:$PORT wsgi:app``; this file
only changes how many requests the process can serve at once.

Gunicorn's default is one synchronous worker, which serves exactly one request
at a time. A single slow call — a Daraja request can legitimately take its
full 15-second timeout — then stalls every shopper behind it. Threads fix that
cheaply: most of a request's time is spent waiting on PostgreSQL or the
network, which releases the GIL.

Sizing: connections needed are at most workers × (pool_size + max_overflow) =
2 × 10 = 20, and the rate limiter's own connection comes out of the same pool.
Override with WEB_CONCURRENCY / GUNICORN_THREADS rather than editing this file.
"""

import os

worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# Longer than the slowest legitimate request (Daraja's 15 s plus a database
# round trip), short enough that a hung worker is replaced.
timeout = 30
graceful_timeout = 20
keepalive = 5

# Keep request smuggling-style oversize headers out before Flask sees them.
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# Logs go to stdout/stderr, where the platform collects them.
accesslog = "-"
errorlog = "-"
