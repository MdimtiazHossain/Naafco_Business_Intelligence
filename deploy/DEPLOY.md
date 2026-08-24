# Deploying to a Hostinger VPS with Supabase

The database is Supabase; everything else runs in three containers on the VPS
behind Caddy, which terminates TLS for both hostnames. No warehouse data is
stored on the VPS.

```
browser ──► VPS :443 ──► Caddy ─┬─ app.example.com ─► nginx    (static bundle)
                                └─ api.example.com ─► uvicorn  (FastAPI)
                                                          │
                                                          ▼
                          Supabase ── Supavisor pooler ── PostgreSQL
```

Nothing but Caddy is reachable from the internet: neither the API nor the web
bundle publishes a port.

---

## 0. What you need first

| | |
|---|---|
| Hostinger VPS | Ubuntu 22.04/24.04, 2 vCPU / 4 GB minimum. An import holds one transaction over ~200k staging rows; 2 GB is not enough headroom. |
| Two DNS A records | `app.example.com` and `api.example.com`, both pointing at the VPS IP, **resolving before you start the stack** — Caddy proves them over HTTP on port 80 and cannot prove a name that does not point here yet. |
| A Supabase project | See the sizing note below. |
| `data/Master Data.xlsx` | Copied onto the VPS. It is mounted read-only. |

### Supabase plan

The existing SQLite database holds ~434,000 rows across 47 tables; live payload
is about 155 MB, dominated by `stg_target` (83 MB) and `fact_target` (47 MB).
On PostgreSQL, with indexes, expect **350–450 MB and growing with every upload**.

The free tier is 500 MB **and pauses a project after 7 days of inactivity**.
You would start at roughly 80% of the cap, and a paused database is a dead
platform. **Pro (8 GB) is the plan this data actually fits.**

---

## 1. Supabase project

Create the project, then from *Project Settings → Database → Connection string*
collect **both** pooler URLs. Which one is which matters:

| Purpose | Mode | Port | Used by |
|---|---|---|---|
| The application | transaction | **6543** | `DATABASE_URL` |
| Migrations and the data copy | session | **5432** | `DIRECT_URL` |

The *direct* connection (`db.<ref>.supabase.co`) is IPv6-only unless the project
buys the IPv4 add-on, so a normal IPv4 VPS cannot use it at all. That is why
both URLs above are pooler URLs.

Rewrite each one for SQLAlchemy: the scheme must be `postgresql+psycopg://`, and
append `?sslmode=require`.

Nothing about Supabase Auth, Storage or the JS client is used. Supabase is the
database. Authentication, roles, sections and data scope stay in this
application's own tables, and the browser never talks to Supabase.

---

## 2. Prepare the VPS

```bash
ssh root@<vps-ip>
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable

git clone <your-repo-url> /opt/ai-business-agent
cd /opt/ai-business-agent/deploy
cp .env.production.example .env.production
chmod 600 .env.production
```

Fill in `.env.production`. Generate the JWT secret rather than inventing one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Check before continuing: `APP_ENV=production`, `ALLOW_HEADER_AUTH=false`,
`CORS_ORIGINS` is exactly your app origin (never `*`), and `VITE_API_BASE_URL`
matches `API_DOMAIN`.

Copy the workbook up:

```bash
scp "data/Master Data.xlsx" root@<vps-ip>:/opt/ai-business-agent/data/
```

---

## 3. Build the schema on Supabase

Migrations run over `DIRECT_URL`, and **Alembic refuses to run over port 6543**
— DDL and Alembic's version lock both need one connection held for the whole
migration, and transaction pooling does not promise one. A half-applied revision
on a live warehouse is what that refusal exists to prevent.

```bash
cd /opt/ai-business-agent/deploy
docker compose -f docker-compose.prod.yml build backend
docker compose -f docker-compose.prod.yml run --rm \
  -w /app/backend backend alembic upgrade head
```

Confirm it landed at head (`0024_customer_name_on_views` at the time of writing).

---

## 4. Copy the data across

Run this **from the machine that holds `data/dev.db`**, not the VPS, with
`DIRECT_URL` set to the session-mode pooler.

Dry run first — it writes nothing and reports what it would copy:

```bash
python scripts/migrate_sqlite_to_postgres.py --dry-run
```

Then the real copy:

```bash
python scripts/migrate_sqlite_to_postgres.py
```

What it guarantees, and why you can re-run it safely:

- It refuses unless **both** databases are at the same Alembic head and **every
  target table is empty**. It has no upsert and no merge; it only ever fills an
  empty schema.
- The whole copy is **one transaction**. A failure at 90% leaves Supabase as
  empty as it started.
- Rows are read and written through the same SQLAlchemy `Table` objects, so JSON
  decodes and re-encodes as `JSONB`, `0`/`1` becomes `false`/`true`, and an ISO
  string becomes a `timestamp` — the column types do the conversion, not the
  script. Codes stay strings; surrogate keys are copied verbatim.
- If a table yields fewer rows than were counted moments earlier, it **aborts** rather
  than continuing: something is writing to the source, and a target assembled from a
  moving source matches no point in time at all.
- Afterwards it advances every identity sequence past the largest copied key. Without
  that step the next upload would collide with row 1.
- It re-counts both sides and **rolls the entire copy back** on any disagreement.

If it aborts, read the message: every failure says what is wrong *and* what to
do about it.

---

## 5. Start the stack

```bash
cd /opt/ai-business-agent/deploy
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f caddy   # watch certificates issue
```

First boot does two things on its own, both idempotent: it seeds a system-default
marker design for every entity type, and it fails any import batch a restart left
mid-flight.

---

## 6. Verify

```bash
curl -si https://api.example.com/health        # 200, and connectivity only
curl -sI https://app.example.com | head -1     # 200
```

Then in a browser, in this order — each one exercises a different layer:

1. **Log in.** Proves JWT signing, the user tables and CORS.
2. **Open the dashboard.** Proves the tool path: `routes_dashboard` → `execute_tool`
   → permission filter → the `vw_*` views.
3. **Open the Business Map.** Proves the static `geo/*.geojson` assets are served
   and `/api/map/marker-config` answers.
4. **Ask the assistant a question** whose answer you can check against a page.
   They cannot disagree — both take the same path — so a disagreement means a
   deployment problem, not a rounding one.
5. **Upload a small file.** Proves the writable volume, the background job pool
   and the progress endpoint. This is the step that fails if `TRANSACTIONS_DIR`
   is wrong (see below).
6. **Export a result.** Proves the export path re-reads under the caller's identity.

---

## Things that will bite

**Uploads and the read-only mount.** `upload.files.upload_dir()` derives its
staging directory from `TRANSACTIONS_DIR`, and `data/` is mounted read-only to
keep the source workbook read-only. The compose file therefore points
`TRANSACTIONS_DIR` and `REPORTS_DIR` at `/app/var`, a named volume. Move them
back under `/app/data` and every upload fails on a read-only filesystem.

**`VITE_API_BASE_URL` is baked into the bundle at build time.** Changing it means
`docker compose -f docker-compose.prod.yml build frontend`, not a restart.

**`prepared statement "_pg3_0" does not exist`.** The application is not on port
6543, or something bypassed `get_engine()`. `connection._engine_options` disables
psycopg's prepared statements for that port precisely because transaction pooling
may hand the next transaction a different server connection.

**Certificates fail to issue.** DNS does not point here yet, or port 80 is
closed. Caddy needs 80 open permanently, not just once — renewals use it too.

**Losing the `caddy_data` volume** means re-issuing every certificate, and
Let's Encrypt rate-limits that. Keep it.

---

## Routine operations

Redeploy after a code change:

```bash
cd /opt/ai-business-agent/deploy
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

If the change includes a migration, run it **before** bringing the new code up,
using the `alembic upgrade head` command from step 3.

Backups: Supabase takes them on a schedule (daily on Pro), and a manual one is
`Database → Backups`. The VPS holds no warehouse data — only staged upload files
on `backend_var`, which are re-uploadable by definition.

Rollback: `git checkout <previous-tag> && docker compose -f docker-compose.prod.yml up -d --build`.
A **migration** rollback is a different matter — several revisions refuse to run
while the tables they would drop hold rows, deliberately, so read the revision
before downgrading anything.

Logs:

```bash
docker compose -f docker-compose.prod.yml logs -f backend
```
