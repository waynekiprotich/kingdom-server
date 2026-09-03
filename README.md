# The Kingdom Collection — Backend

Flask REST API for The Kingdom Collection. Guest checkout, M-Pesa payments,
PostgreSQL.

## Requirements

| Tool | Version used |
| ---- | ------------ |
| Python | 3.14.7 |
| PostgreSQL | 18.6 (`brew install postgresql@18`) |

PostgreSQL 18 runs as a Homebrew service (`brew services start postgresql@18`).
Its binaries are keg-only, so add them to your PATH:

```bash
export PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH"
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env      # then fill in DATABASE_URL, SECRET_KEY, JWT_SECRET_KEY
```

Generate each secret with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

The local databases already exist (`kingdom_collection` and
`kingdom_collection_test`, owned by the `kingdom` role) and the initial
migration is applied. On a fresh machine:

```bash
createuser kingdom
createdb -O kingdom kingdom_collection
createdb -O kingdom kingdom_collection_test
FLASK_APP=wsgi.py .venv/bin/python -m flask db upgrade
FLASK_APP=wsgi.py .venv/bin/python -m flask create-admin
```

`create-admin` prompts for the password rather than taking it as an argument,
so it never lands in shell history.

## Commands

| Command | Does |
| ------- | ---- |
| `flask run --port 8000` | Development server on :8000 |
| `flask seed-dev --reset` | Load development catalog data (development only) |
| `flask db migrate -m "..."` | Autogenerate a migration from model changes |
| `flask db upgrade` | Apply migrations |
| `flask db downgrade` | Roll back one migration |
| `flask create-admin` | Create an admin account (prompts for the password) |
| `pytest` | Run the test suite |

Every command needs `FLASK_APP=wsgi.py` in the environment (or a `.env`).

**Use port 8000, not 5000.** macOS ControlCenter (AirPlay Receiver) listens on
`*:5000`, so a Flask server bound there is shadowed and requests to
`localhost:5000` come back `403 Forbidden` from AirPlay instead.

## Endpoints

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET | `/health`, `/health/ready` | Liveness; readiness also checks the database |
| GET | `/api/products` | Filter, search, sort, paginate. See below |
| GET | `/api/products/<slug>` | Detail with variants, images and options |
| GET | `/api/categories` | Categories with active product counts |
| POST | `/api/admin/auth/login,refresh,logout` | Admin only |
| GET | `/api/admin/auth/me` | Admin only |
| POST | `/api/admin/images/upload-signature` | Signs a direct Cloudinary upload |
| GET POST | `/api/admin/categories` | List (with counts) and create |
| GET PATCH DELETE | `/api/admin/categories/<id>` | Delete is superadmin-only |
| GET POST | `/api/admin/products` | List includes drafts and archived; filters `status`, `q`, `category_id`, `sort` |
| GET PATCH DELETE | `/api/admin/products/<id>` | `DELETE` archives; `?permanent=true` is superadmin-only |
| POST | `/api/admin/products/<id>/variants` | |
| PATCH DELETE | `/api/admin/variants/<id>` | |
| POST | `/api/admin/products/<id>/images` | Attach an already-uploaded `public_id` |
| PATCH DELETE | `/api/admin/images/<id>` | |

### Admin catalog rules

- **Deletion preserves history.** `DELETE` on a product archives it. Permanent
  deletion is superadmin-only and refused once the product appears in an order.
  A variant that has been ordered is deactivated rather than deleted.
- **Stock only moves through the ledger.** Every change to `stock_quantity`,
  including a variant's opening stock, writes an `inventory_movements` row with
  the signed delta and the admin who made it (§19).
- **Every mutation writes an audit row** in the same transaction as the change,
  with the before and after values, so the trail cannot disagree with the data.
- **Unknown request fields are rejected** with 422. A typo like `stock_quantiy`
  fails loudly rather than being silently ignored.
- **PATCH distinguishes absent from null.** Omitting a field leaves it alone;
  sending `null` clears it. `MISSING` in `app/validation.py` is what makes that
  possible.
- **Slugs**: one you supply is never silently altered — a clash is a 409. One
  we generate from the name gets a numeric suffix instead.
- Roles: any active admin may create and edit. Superadmin is required to delete
  a category or permanently delete a product.

`GET /api/products` accepts `category` (slug), `q`, `size`, `color`,
`min_price`, `max_price`, `in_stock`, `featured`, `sort`
(`newest|price_asc|price_desc|name`), `page`, `per_page` (max 48). An unknown
value is a `422 VALIDATION_ERROR`, not a silent fallback. The response carries
`products`, `pagination` and `filters` — the last of which lists the sizes,
colours and price range in scope, so the storefront builds its filter controls
from real data rather than a hard-coded list.

## Structure

```
server/
├── app/
│   ├── __init__.py       create_app factory
│   ├── config.py         Environment-driven config; production fails fast
│   ├── extensions.py     db / migrate / jwt / cors singletons
│   ├── errors.py         ApiError types and the §14 error envelope
│   ├── validation.py     Request body helpers
│   ├── jwt_callbacks.py  JWT failures rendered in the same envelope
│   ├── cli.py            flask create-admin
│   ├── models/           SQLAlchemy models, one module per domain area
│   └── blueprints/       One module per resource group
├── migrations/           Alembic
├── tests/
└── wsgi.py               gunicorn wsgi:app
```

## Conventions

- **Errors.** Every failure returns
  `{"success": false, "error": {"code": "...", "message": "..."}}`. Raise an
  `ApiError` subclass from `app.errors`; never return a bare string. Clients
  branch on `code`, so codes are stable and messages are not.
- **Money is `Numeric(12, 2)`**, never float. Amounts are KES.
- **Constraints over conventions.** Rules that must hold under concurrency live
  in the schema: `stock_quantity >= 0`, a unique `payments.mpesa_receipt_number`,
  positive order-item quantities. Application checks are a courtesy on top.
- **Statuses are `StrEnum` + a CHECK constraint**, not native PostgreSQL enums —
  adding a value should not need a migration. Note that SQLAlchemy 2 omits that
  constraint unless `create_constraint=True` is passed; `status_enum()` in
  `app/models/base.py` does, and a test asserts the database rejects a bad
  status.
- **Alembic cannot see those enum CHECK constraints.** They are emitted at DDL
  time and are not comparable objects in the metadata, so autogenerate reads
  them as "removed" and writes a `DROP` into *every* migration — silently
  undoing the guard above. `migrations/env.py` filters them out via
  `include_object`. If you ever see `drop_constraint('order_status', ...)` in a
  generated migration, that filter has stopped working; do not apply it.
- **Money crosses the wire as a decimal string**, never a float. Serializers
  emit `"3600.00"`.
- **Order status moves through `ORDER_TRANSITIONS`** (`app/models/order.py`).
  A delivered order cannot go back to pending, and `PAID` is only reachable
  from `PAYMENT_PENDING`.

## Security notes

- Production refuses to start without `SECRET_KEY`, `JWT_SECRET_KEY`,
  `DATABASE_URL` and `CORS_ORIGINS`, and rejects signing keys under 32 bytes.
- A wrong email and a wrong password return byte-identical responses — no user
  enumeration. Accounts lock for 15 minutes after 5 failed attempts.
- Logout revokes the presented token via `token_blocklist`; a stolen token does
  not stay valid until expiry.
- Unhandled exceptions log in full and return nothing but a generic message.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests run against `TEST_DATABASE_URL` when it is set (it is, in `.env`, pointing
at `kingdom_collection_test`) and fall back to in-memory SQLite when it is not.
Prefer the PostgreSQL run: constraints, `ondelete` rules and `Numeric` precision
are what these tests assert, and SQLite only approximates them. The suite
recreates the schema per test, so the test database is disposable.

## Images

Cloudinary is used for delivery and for uploads, without the SDK — the only
things needed are URL building and one signature, in
`app/services/cloudinary.py`.

Uploads are **signed direct uploads**: the admin client asks for a signature,
then posts the file straight to Cloudinary. Image bytes never pass through this
server, and the API secret never leaves it.

`CLOUDINARY_CLOUD_NAME` is set to `demo` in development so the delivery
pipeline (transformations, `srcset`, `f_auto`) runs against real URLs before
the business has its own account. Point it at the real cloud and re-upload when
photography exists.

## Status

Backend core and the public catalog are in place against a live database:
app factory, config, all §11 models, the error contract, admin JWT auth, and
the product/category endpoints with filtering, search, sorting and pagination.

Not built yet: order creation, M-Pesa integration, admin CRUD endpoints beyond
the image signature. Those are their own phases.
# kingdom-server
