# Local production deployment — http://localhost

The whole platform on one Windows machine, in the production shape: a real
PostgreSQL, the API behind a reverse proxy, and the React bundle served as static
files. Everything binds to the loopback interface only.

```
browser (this PC, or any PC on the LAN)
   │
   ▼
nginx        0.0.0.0:80      serves frontend/dist, proxies /api/* and /health
   │                         ── the ONLY listener reachable off this machine
   ▼
uvicorn      127.0.0.1:8000  app.main:app, one worker, APP_ENV=production
   │
   ▼
PostgreSQL   127.0.0.1:5432  database ai_business_agent, owned by role ai_agent
```

**Only nginx leaves the machine.** uvicorn and PostgreSQL are bound to the
loopback address, so they cannot accept a connection from the network at all —
that is a property of the binding, not of the firewall, and it holds even if a
firewall rule were added by mistake. Access is bounded by the Windows Firewall
rule `AI Business Agent HTTP`: TCP/80 inbound, **Private** profile,
`RemoteAddress 172.16.1.0/24`. Narrowing or disabling that rule is how LAN access
is removed; the listener itself does not need changing.

The `Wi-Fi` adapter is classified **Private** for this to work — a Private-profile
rule is inert while the active network is classified Public. Revert with
`Set-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -NetworkCategory Public`.

A separate pre-existing rule named **`WWW`** (TCP/80, *Public* profile,
`remote=Any`) is present on this machine. It was not created by this deployment
and has been left alone; it does not govern LAN access while Wi-Fi is Private,
but it would open port 80 broadly if the adapter were ever reclassified Public.

This is **separate from the development instance** on ports 8010 (API) and 5183
(Vite), which still runs on SQLite from the repo-root `.env` and is unaffected by
anything here. Both can run at the same time.

---

## How the configuration is kept apart

`app/config.py` reads only the repo-root `.env`, and it reads it with
`os.environ.setdefault` — so **a real environment variable always wins over that
file**. That single fact is what makes two environments possible without editing
either one:

* the dev instance gets its SQLite settings from `.env`, as it always did;
* this deployment exports `deploy/local/.env.production` as real environment
  variables before starting the process, overriding every value it names.

`.env.production` is **not** loaded automatically. The launchers below load it
(`env.ps1`), and `install-services.ps1` copies it into the service definition.
After editing it, restart whatever consumes it.

---

## What is installed on this machine

| Component | Version | Where | Service |
|---|---|---|---|
| PostgreSQL | 17.11 | `C:\Program Files\PostgreSQL\17` | `postgresql-x64-17`, Automatic |
| nginx | 1.31.4 | `deploy/local/nginx/` (copy of the winget install) | `AIBusinessAgentWeb`, Automatic |
| NSSM | 2.24 | `deploy/local/nssm.exe` | — (the service supervisor) |
| Python | 3.12.10 | `.venv/` at the repo root | — |
| Node | 24.15.0 | system | — (build only) |

PostgreSQL was set to `listen_addresses = 'localhost'` (`ALTER SYSTEM`), so the
port is not open to the network. nginx and uvicorn bind `127.0.0.1` explicitly.

nginx is a **copy** rather than the winget path because that path carries the
version number (`…\nginx-1.31.4\`) and a service pointing at it would break on
the next upgrade. Same reason for `nssm.exe`.

**Neither binary is committed** — `deploy/local/nginx/` and `deploy/local/nssm.exe`
are both ignored, because a third-party executable is not source for this project
and would sit in the repository's history for good. A fresh clone has to put them
in place before `install-services.ps1` can run: install nginx (`winget install
nginx`, or <https://nginx.org/en/download.html>) and copy the distribution
directory to `deploy/local/nginx/`, and unpack NSSM 2.24
(<https://nssm.cc/download>) so that the `nssm.exe` from its `win64`
directory lands at `deploy/local/nssm.exe`.

---

## Files here

| File | Purpose | Committed |
|---|---|---|
| `.env.production` | Every setting this deployment overrides | no (`.env.*` is gitignored) |
| `credentials.generated.txt` | The generated PostgreSQL and JWT secrets | no |
| `env.ps1` | Shared loader, dot-sourced by the others | yes |
| `migrate.ps1` | `alembic upgrade head` against PostgreSQL | yes |
| `copy-data.ps1` | SQLite → PostgreSQL row-for-row copy | yes |
| `start-backend.ps1` | Run the API in the foreground | yes |
| `start-nginx.ps1` | Run / test / reload / stop nginx | yes |
| `nginx.conf` | The reverse-proxy and SPA config | yes |
| `security-headers.conf` | Response headers, included per location | yes |
| `install-services.ps1` | Register both Windows services | yes |
| `uninstall-services.ps1` | Remove them again | yes |
| `install-postgres.ps1` | The unattended PostgreSQL install | yes |
| `nginx/`, `run/`, `*.log` | Binaries and runtime output | no |

**`credentials.generated.txt` holds the only copy of the database password and
the JWT signing secret.** Losing the JWT secret only forces everyone to sign in
again; losing the database password means resetting the role as the `postgres`
superuser.

---

## Everyday operation

The services are `SERVICE_AUTO_START`, so **after a Windows restart the stack
comes back on its own** — PostgreSQL first, then the API (which declares a
dependency on it), then nginx (which depends on the API). Nothing needs a
terminal left open.

```powershell
# Status of the whole chain
Get-Service postgresql-x64-17, AIBusinessAgentAPI, AIBusinessAgentWeb

# Restart the API (elevated) — after editing .env.production or anything
# under backend/. The service runs uvicorn without --reload, so a code change
# reaches it only this way.
Restart-Service AIBusinessAgentAPI

# …or, when you want it checked rather than assumed. Same restart, but it
# asserts elevation first, refuses while an import is running, and then proves
# the process actually changed — `Restart-Service` fails quietly if the shell
# is not elevated, and `Get-Service` says Running either way.
.\deploy\local\restart-api.ps1

# Reload nginx after editing nginx.conf (no dropped connections)
powershell -ExecutionPolicy Bypass -File deploy\local\start-nginx.ps1 -Test
Restart-Service AIBusinessAgentWeb
```

Logs: `deploy/local/run/logs/` — `api-stdout.log`, `api-stderr.log`,
`nginx-stderr.log` from NSSM, plus nginx's own `access.log` and `error.log`.
They rotate at 10 MB.

### After changing frontend code

Vite inlines the API base URL at build time, so the bundle must be rebuilt.
nginx serves `frontend/dist` directly, so no service restart is needed.

```powershell
cd frontend
npm run build
```

### After changing backend code

```powershell
Restart-Service AIBusinessAgentAPI   # elevated
```

### After adding a migration

```powershell
powershell -ExecutionPolicy Bypass -File deploy\local\migrate.ps1
Restart-Service AIBusinessAgentAPI
```

Back the database up first — `pg_dump`, not a file copy:

```powershell
$env:PGPASSWORD = '<ai_agent password from credentials.generated.txt>'
& 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe' -h localhost -U ai_agent `
    -d ai_business_agent -Fc -f "D:\MIH\AI_Business_Agent\data\pg-backup-$(Get-Date -f yyyyMMdd).dump"
```

---

## Running it by hand instead

Useful when something is wrong and you want to watch it happen. Stop the
services first, or the ports are taken.

```powershell
Stop-Service AIBusinessAgentWeb, AIBusinessAgentAPI    # elevated, Web first

powershell -ExecutionPolicy Bypass -File deploy\local\start-backend.ps1   # blocks
powershell -ExecutionPolicy Bypass -File deploy\local\start-nginx.ps1     # blocks
```

---

## First-time setup, in order

Already done on this machine; recorded so it can be repeated.

```powershell
# 1. PostgreSQL (elevated, unattended)
powershell -ExecutionPolicy Bypass -File deploy\local\install-postgres.ps1 <superuser-password>

# 2. Role and database — UTF8 from template0, so the cluster's own locale does
#    not decide how Bangla is stored.
#      CREATE ROLE ai_agent LOGIN PASSWORD '...';
#      CREATE DATABASE ai_business_agent OWNER ai_agent
#        ENCODING 'UTF8' TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C';

# 3. Schema: 24 revisions, 48 tables, 8 views
powershell -ExecutionPolicy Bypass -File deploy\local\migrate.ps1

# 4. Data. Preflight refuses unless both sides are at the same head and every
#    target table is empty — migrations 0010/0017 seed 4 rows into
#    map_area_styles, so clear those first (they are identical to the source's).
powershell -ExecutionPolicy Bypass -File deploy\local\copy-data.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File deploy\local\copy-data.ps1

# 5. Frontend bundle
cd frontend; npm run build; cd ..

# 6. Services (elevated)
powershell -ExecutionPolicy Bypass -File deploy\local\install-services.ps1
Start-Service AIBusinessAgentAPI; Start-Service AIBusinessAgentWeb
```

---

## Things that will bite

**The bundle has the API URL baked in.** `frontend/.env.production` sets
`VITE_API_BASE_URL=/`, which `apiClient.ts` reduces to `''` — every request is a
relative path on the same origin, so no CORS is involved at all. Building with
`.env.local` in force instead would bake in `http://127.0.0.1:8010` and the
deployed app would silently talk to the *dev* API. Check after a build:

```powershell
Select-String -Path frontend\dist\assets\*.js -Pattern '127\.0\.0\.1:8010' -SimpleMatch
```
No output is the correct result.

**`/health` is not under `/api`.** It is declared in `main.py`, not in a router,
and the frontend polls it for the connection indicator. `nginx.conf` proxies it
with its own `location = /health`. An `/api`-only proxy rule returns `index.html`
for it and the app reports the API down while it is running.

**Uploads have two ceilings and they must stay in this order.** The application
refuses above 25 MB (`upload/files.py`, `MAX_UPLOAD_BYTES`); nginx refuses above
32 MB (`client_max_body_size`). nginx's has to be the larger one, or a legitimate
20 MB workbook becomes a bare nginx `413` that the application never sees and
cannot explain.

**One uvicorn worker, and not a number to tune.** Live import progress is held in
process memory (`utils/progress.py`) because an import is one transaction and a
row written inside it is invisible until commit. With two workers, a progress
poll can land on the other process and report "unknown job" for an import that is
running perfectly well. The import thread pool is per-process too, so a second
worker would quietly double `IMPORT_WORKER_COUNT`.

**The services run as LocalSystem**, so files staged by an import under
`data\uploads` are owned by SYSTEM rather than by you. Running as your own
account would require that account's password in the service definition.

**Serving the LAN needs no CORS change.** An earlier version of this file said it
did; that was wrong. `frontend/.env.production` sets `VITE_API_BASE_URL=/`, so
every request is a relative path — a browser at `http://172.16.1.19` asks
`172.16.1.19` for `/api/...`, which is **same-origin**. CORS governs
cross-origin requests only, so `CORS_ORIGINS` is never consulted on that path and
was left untouched. It would only matter if something called the API on a
different origin than the page it was served from.

**Taking an nginx upgrade** is `winget upgrade nginxinc.nginx` followed by
re-copying the tree into `deploy/local/nginx/`; the service path never changes.

---

## Portability fixes this deployment required

The schema had only ever been built on SQLite, and four latent bugs surfaced the
first time the migrations ran on PostgreSQL. All four are fixed in the migration
sources and are no-ops on SQLite:

| Where | Problem |
|---|---|
| `0016`, `0018`, `0019`, `0021` | `WHERE f.is_void = 0` — SQLite treats booleans as integers, PostgreSQL rejects `boolean = integer`. Now `= FALSE`, matching what every other view already used. |
| `0017_admin_layers` | `AND is_system_default = 1`, same cause. Now `= TRUE`. |
| `0020_remove_receivables_and_warehouse` | A guard tested the **bigint** `warehouse_id` against `''`. The clause never excluded a row on either dialect; it is now applied only to the text column. |
| `migrations/env.py` | Alembic hard-codes `alembic_version.version_num` as `VARCHAR(32)`; the longest revision id here is 42 characters. SQLite ignores a declared length, PostgreSQL enforces it and refused the stamp *after* the revision's DDL had succeeded. `_widen_version_table` now widens it to 128 before migrations run. |

These are equally required by `deploy/DEPLOY.md`'s Supabase deployment, which had
never been run either.
