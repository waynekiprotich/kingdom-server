# Operations

Running The Kingdom Collection in production. Everything here is a task on a
dashboard or a command you run — the code-level rules live in `CLAUDE.md`.

---

## Investigating a failure a customer reported

Every request carries an id. It appears in three places:

- the `X-Request-Id` response header,
- every log line produced while handling that request,
- the `reference` field of a 500 response body.

So when a customer says "it failed when I tried to pay":

1. Ask for the reference shown on screen (or read `X-Request-Id` from their
   browser's network panel).
2. Search Render → your service → **Logs** for that string.
3. You get the traceback, the HTTP method, and the path, all on one line.

A request that arrives with an `X-Request-Id` already set (Render stamps some)
keeps that value, so the platform's own logs and ours name the same request.

**Deliberately not sent to the customer:** the exception type, the message, or
any stack. The reference identifies a failure without describing it.

---

## Database backups

**Verify this before launch. Render's free Postgres tier has no backups at
all**, and losing order history cannot be undone — an order is the only record
that a customer paid you.

1. Render dashboard → your Postgres instance → **Backups**.
2. Confirm the plan actually includes automated backups, and note the
   retention window.
3. If it does not, either upgrade the instance or schedule the manual dump
   below somewhere that is not the same machine.

Manual dump (also worth taking by hand before any risky migration):

```bash
pg_dump "$DATABASE_URL" --format=custom --file="kingdom-$(date +%F).dump"
```

Restore into a fresh database:

```bash
pg_restore --dbname="$TARGET_DATABASE_URL" --clean --if-exists kingdom-YYYY-MM-DD.dump
```

Test a restore at least once. A backup nobody has restored is a hypothesis.

---

## Uptime monitoring

Two endpoints, and they answer different questions. Monitor both.

| Endpoint | Answers | Alert means |
|---|---|---|
| `/health` | Is the process up? Touches nothing. | The service is down or asleep. |
| `/health/ready` | Can it serve traffic? Runs `SELECT 1`. | Process is up but the database is unreachable. |

Point UptimeRobot (or Better Uptime, or Render's own) at `/health/ready` on a
1–5 minute interval — it catches strictly more than `/health` does. Keep
`/health` as a second, quieter check so you can tell "app is down" apart from
"database is down".

**If you are on Render's free tier**, the service sleeps after ~15 minutes of
inactivity and the next request takes 30–60 seconds. To a customer that is
indistinguishable from a broken shop. Either move to a paid instance or accept
that an uptime monitor pinging every 5 minutes is also what keeps it awake.

---

## Environment variables

`ProductionConfig` refuses to start without the first four. None are committed
— see `.env.example`.

| Variable | Notes |
|---|---|
| `SECRET_KEY` | ≥32 bytes. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `JWT_SECRET_KEY` | As above, and a *different* value. |
| `DATABASE_URL` | From the Render Postgres instance. |
| `CORS_ORIGINS` | Comma-separated. Must match the deployed frontend origin **exactly**. |
| `TRUSTED_PROXY_HOPS` | `1` on Render (the default in `ProductionConfig`). |
| `CLOUDINARY_CLOUD_NAME` | The account cloud name, not a folder name. |
| `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Signing uploads. Never `VITE_`-prefixed. |

### CORS, specifically

The single most common cause of "the shop loads but nothing appears".

The storefront runs on **Cloudflare Workers**, and the value currently set on
Render is:

```
CORS_ORIGINS=https://kingdom-client.waynekip123.workers.dev
```

An origin is scheme + host only. **No trailing slash, no path** —
`https://…workers.dev/` with the slash does not match and the browser blocks
every request.

Verify it from anywhere, without a browser:

```bash
curl -sI -H "Origin: https://kingdom-client.waynekip123.workers.dev" \
  https://kingdom-server-ao5x.onrender.com/api/products | grep -i access-control-allow-origin
```

The origin you sent should come back. Silence means the browser will block
the shop.

Cloudflare also serves each deployed version on its own preview hostname
(a version prefix on the same `workers.dev` subdomain). Those are *not*
covered by the production origin above, so a preview build will fail CORS
even though the live site works — that is expected, not a bug to chase. Once
there is a custom domain, point `CORS_ORIGINS` at it and redeploy; nothing
else in the backend references the frontend's address.

---

## Deploying

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn --bind 0.0.0.0:$PORT wsgi:app`
- **Pre-deploy command:** `FLASK_APP=wsgi.py flask db upgrade`
- **Python:** pinned to 3.12.7 in `runtime.txt`.

**Migrations are not automatic.** Any deploy that includes a new file in
`migrations/versions/` needs the pre-deploy command above to have run, or
every request touching the changed table will fail. Check for one before
deploying:

```bash
git diff --name-only origin/main -- migrations/versions/
```

The frontend deploys separately (Cloudflare Workers) and needs its own rebuild
whenever `VITE_API_URL` changes — the API preconnect tag is injected at build
time, so it only appears in a fresh build.

---

## Rate limits

Defined in `app/config.py: RATE_LIMITS` as `name -> (requests, window
seconds)`, counted per client address in Postgres.

| Limit | Current | Why |
|---|---|---|
| `login` | 10 / 15 min | On top of per-account lockout. |
| `refresh` | 60 / 15 min | A normal session refreshes rarely. |
| `orders` | 40 / 10 min | Sized for **shared** addresses, not one shopper — see below. |
| `upload-signature` | 60 / hour | Admin-only, and each one is a single photograph. |

**The orders limit is not "how many orders one person places."** Kenyan
carriers put large numbers of mobile customers behind a single public address
(CGNAT), so everyone shopping over one carrier's mobile data shares this
allowance. Lowering it to a number that sounds sensible for an individual will
start turning away real customers on a busy evening.

Two properties of the limiter not to break: counts are written on their own
connection, so a *rejected* request still counts; and it fails open, so a
missing table degrades to "no limiting" rather than a dead API.

`TRUSTED_PROXY_HOPS` must match the number of proxies actually in front of the
app. Trusting more hops than exist lets a caller forge their address through
`X-Forwarded-For` and bypass all of the above.

---

## Before launch

- [ ] Replace the dev admin accounts (`dev-admin@kingdom.local`,
      `dev-staff@kingdom.local`) with real ones via `flask create-admin`.
- [ ] Confirm `SECRET_KEY` and `JWT_SECRET_KEY` are freshly generated, not
      carried over from development.
- [ ] Confirm database backups exist, and restore one to prove it.
- [ ] Point an uptime monitor at `/health/ready`.
- [x] `CORS_ORIGINS` set and verified against the live Workers origin. Revisit
      only when a custom domain replaces it.
- [ ] Replace seeded demo products with the real catalogue.
- [ ] Privacy policy, terms, and returns policy published — you process phone
      numbers and payments, so Kenya's Data Protection Act applies.
- [ ] ODPC registration considered.
