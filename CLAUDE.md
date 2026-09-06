# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Bangladeshi FMCG business-intelligence platform: a PostgreSQL star-schema warehouse fed by an ETL pipeline, a natural-language (English / Bangla / mixed) reporting agent over it, and a React web platform. Built in phases whose numbering still labels most modules and docstrings:

| Phase | Scope | Lives in |
|---|---|---|
| 1 | Master-data dimensions discovered from `data/Master Data.xlsx` | `backend/app/master_data/`, `database/models.py` |
| 2 | Transaction ETL, staging, facts, reporting views | `backend/app/etl/`, `database/models_warehouse.py` |
| 3 | AI agent: intents, tools, permission filtering | `backend/app/ai/` |
| 4 | Auth, web platform, WhatsApp, upload centre, data management | `backend/app/auth/ api/ upload/ datamgmt/`, `frontend/` |

Three packages sit outside that numbering: `app/reporting/` (the `/api/reports/*` query layer and the shared column catalogue), `app/integrations/` (WhatsApp) and `app/targetmgmt/` (Target Management — building a target rather than reporting one).

**A list of layer, metric, section or dataset names must never outlive what it names.** The example that taught this is now itself history: revision 0020 removed Warehouse but left the business map's `DEFAULT_LAYERS` still listing it, and because the map fell back to that list when a caller named no layers and *then* rejected any layer it did not recognise, its main endpoint returned 422 for every default request until it was fixed. A stale name in a default does not degrade to a missing item; it breaks the request. The map has since been removed entirely (`0033_remove_map`), which is the same rule at a larger scale — see that section for what stayed and why. When removing a dataset or a feature, grep for its name across defaults, style tables, KPI keys, metric lists, section keys and parametrised tests: the registries derive themselves, but hand-written lists like these do not.

**Three datasets left the platform in revision 0020: Collection, Outstanding and Warehouse.** None held a row — no receivables extract was ever produced and no Warehouse Master ever arrived — and the material architecture (Plant, Storage Location, Material) is what locates stock now. Warehouse is gone for good: stock is located by Plant and Storage Location, and there is no warehouse anywhere. Some of `README.md` still describes all three; where it does, this file wins.

**Receivables came back in revision 0031, and the reasoning that removed it is what lets it return.** 0020's premise was that nothing could measure the figures — not that receivables were uninteresting. That premise has changed: a Credit Invoice file exists, stating the invoice, its terms and every amount posted against it, so what is outstanding is now *read* rather than estimated. Credit Control is therefore a genuine reversal of a documented decision rather than a new report, and the reversal is deliberate and approved. **Collection did not come back** — the source aggregates payments into one `payment_amount` and a Last Payment Date, so there is no per-transaction history to hold and none is invented; the invoice detail panel shows a single payment event and says why. The transactional surface is now **sales, material stock, target and credit invoice**. Aging, overdue and outstanding are real questions again, over `fact_credit_invoice` and nothing else — there is still no `fact_outstanding` and no `fact_collection`, and a question about *collections* is still answered "this system does not track that".

**The assistant answers receivables questions again** (`get_credit_summary`, `get_credit_aging`, `get_overdue_customers`, intents `CREDIT_*`, metric family `credit` in `intent.py`), and `HIGH_OVERDUE` is back on `get_business_alerts`. The prompt was changed in the same step that gave the agent the tools, never before: removing an honest "not tracked" while the agent still had nothing to answer with would have replaced a refusal with a guess.

**Collections did not come back, and the distinction is the one worth guarding.** There is no `COLLECTION` intent, no collection tool, and `collection` is deliberately absent from `METRIC_KEYWORDS` — a metric word the agent recognises but cannot answer routes a question to nothing, which is worse than not recognising it. No extract states individual payment transactions, so an invoice's aggregate `payment_amount` is the only payment figure that exists, and it is *not* a collection. The prompt says so explicitly because that substitution is the cheap mistake here: the number is sitting right there.

**`HIGH_OVERDUE` measures a share, not an amount.** A crore overdue is alarming on a small book and routine on a large one, so a threshold in taka would need re-setting every time the business grew. `AlertToolInput.overdue_share_percent` defaults to 25. The share is suppressed rather than rendered as `0%` when nothing is outstanding — a book with no receivables has no overdue proportion.

**No Overdue card on the executive dashboard, deliberately.** The plan offered one as optional and the scope gap below rules it out: the dashboard is the one screen a regional manager opens by default, `get_business_summary` is scoped to their region, and the credit view cannot honour a region scope. The only two options were to refuse the whole dashboard for them — denying their sales and stock figures over a receivables number they were never going to see — or to put an unscoped national overdue figure on it. The card goes back on the table once the credit view carries the sales hierarchy.

`README.md` (~3165 lines) is the authoritative specification — validation rule catalogue (VR001–VR016), table-by-table schema, endpoint tables, and a numbered "Assumptions and decisions" list explaining *why* each design choice was made. Read the relevant section before changing behaviour it describes.

## Commands

The virtualenv is `.venv/` at the repo root; on this machine use `.\.venv\Scripts\python.exe` explicitly.

There is no CI, no backend linter and no formatter config in this repo — `pytest` and the frontend's `typecheck`/`lint`/`build` are the whole gate, so run them yourself before calling work done.

```powershell
# Tests (1770 backend tests; run from the repo root — pytest.ini sets pythonpath=backend)
.\.venv\Scripts\python.exe -m pytest -n auto --dist loadfile   # pytest-xdist: ~30 min, against ~3 hours serially
.\.venv\Scripts\python.exe -m pytest backend/tests/test_etl_pipeline.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_ai_tools.py::test_name -x

# API
cd backend; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload   # /docs

# Migrations (alembic must run from backend/; the URL comes from app.config, not alembic.ini)
cd backend; ..\.venv\Scripts\alembic.exe upgrade head
cd backend; ..\.venv\Scripts\alembic.exe revision -m "0015_thing"

# Frontend (this machine: API on 8010, Vite on 5183 — see frontend/.env.local)
cd frontend; npm run dev
cd frontend; npm test          # vitest, 22 suites
cd frontend; npm run lint      # oxlint
cd frontend; npm run typecheck # tsc -b --noEmit — the fast gate
cd frontend; npm run build     # tsc -b then vite build — the real typecheck gate

# Postgres
docker compose up -d db
```

**This machine now runs both dialects, and which one you get depends on how the process was started.** Development is SQLite: `.env` sets `DATABASE_URL=sqlite:///.../data/dev.db`, and `start-backend.bat` / `start-frontend.bat` serve it on 8010 / 5183. The localhost deployment is PostgreSQL 17 on 5432, and it overrides `.env` by exporting `deploy/local/.env.production` as real environment variables — which works because `config._load_dotenv` uses `os.environ.setdefault`, so a real variable always beats the file. Neither environment can disturb the other, and `data/dev.db` remains a complete fallback because the copy only ever read it. See `deploy/local/README.md`.

Every migration, query and view is written to be dialect-portable precisely so that works; keep new ones portable (no Postgres-only types or functions without a SQLite path). `data/` holds a `dev.db.preNNNN.bak` for each migration already applied — copy `dev.db` aside before running a new one.

**"Portable" was aspirational until the migrations were actually run on PostgreSQL, and four of them were not.** All four failures share one cause — SQLite does not enforce what it is told, so the mistake left no trace there. `0016`/`0018`/`0019`/`0021` compared `is_void = 0` and `0017` compared `is_system_default = 1`: SQLite stores a boolean as an integer, PostgreSQL rejects `boolean = integer` outright. They now read `= FALSE` / `= TRUE`, which is what every view `0009` authored already used. `0020` tested the **bigint** `fact_sales.warehouse_id` against `''`; that clause never excluded a row on either dialect (SQLite orders every integer before every string) and now applies only to the text column beside it. And `migrations/env.py` had to grow `_widen_version_table`, because Alembic hard-codes `alembic_version.version_num` as `VARCHAR(32)` while the longest revision id here — `0011_customer_subterritory_product_company` — is 42 characters; SQLite ignores a declared length, PostgreSQL enforces it and refused the *stamp* after the revision's own DDL had already succeeded, which rolls the revision back and reads as a migration failure with no failing statement. **A new revision is not portable until it has run on both**, and `deploy/local/migrate.ps1` is the cheap way to find out — but applying a revision to the database that already holds every earlier one proves only that *it* runs. What a new deployment does is run the chain from nothing, and that is where an ordering mistake between revisions would show up and nowhere else. `0001` → head has now been built into an empty PostgreSQL **schema** (the deployment role cannot create databases; a schema is the same test) and comes out at 62 tables and 8 views. Doing it surfaced a real defect: `migrations/env.py` writes the URL through `Config.set_main_option`, whose configparser reads `%` as interpolation syntax — so a password containing an encoded `@`, `/` or `:` refused every migration before a single revision ran. `connection.alembic_ini_value` doubles the percent and `get_main_option` undoes it, which is the escape configparser itself defines; it lives beside `assert_migration_safe` for the same reason that one does, and `test_supabase_migration.py` pins it without running a migration.

Data pipeline scripts (all in `scripts/`, all import `_bootstrap` first to put `backend/` on `sys.path` and force UTF-8 stdout):

```powershell
python scripts/inspect_master_data.py        # profile the workbook -> reports/
python scripts/validate_master_data.py       # exit 0 valid / 1 blocking errors / 2 unreadable
python scripts/import_master_data.py [--dry-run]
python scripts/build_dim_date.py             # idempotent
python scripts/seed_dev_data.py              # local only: fictional org + users + DEMO facts
python scripts/import_transactions.py sales <file> [--dry-run] [--source-system SAP]
python scripts/generate_demo_transactions.py # synthetic facts built only from codes already in the dimensions
python scripts/map_master_data.py [--apply]  # derive customer sub-territory; report-only by default
python scripts/manage_users.py create ceo --role MANAGEMENT --name "..."
python scripts/ask_agent.py "আজকের sales কত?" --user ceo [--interactive]
python scripts/migrate_sqlite_to_postgres.py [--dry-run]   # SQLite -> Supabase, one transaction
python scripts/mine_agent_signals.py [--dry-run] [--limit N]  # sweep stored chats for questions the agent mishandled
python scripts/reload_map_locations.py [--apply] [--file <csv>]  # restore the pre-0033 coordinate export; dry by default
python scripts/clear_data.py <groups>        # DESTRUCTIVE — confirm with the user before running
```

`clear_data.py` (renamed from `clear_demo_data.py`) empties named groups — `masters`, `transactions`, `uploads`, `changelog`, `history`, `users` — and must be told which. It is the one script here that destroys data; never run it speculatively.

## Deployment (Hostinger VPS + Supabase)

`deploy/` holds the production stack — `docker-compose.prod.yml`, a `Caddyfile`,
`.env.production.example` and `DEPLOY.md`, the runbook. Supabase is **the database and
nothing else**: authentication, roles, sections and the four-layer data scope all stay in
this application's own tables, the browser never talks to Supabase, and there is no
Supabase client dependency (`@supabase/supabase-js` was in `frontend/package.json` unused
and has been removed). Three containers run on the VPS — uvicorn, the nginx-served bundle
and Caddy — and only Caddy publishes a port.

**Supabase is reached through two different URLs, and which is which is load-bearing.**
The direct connection is IPv6-only without the paid add-on, so an IPv4 VPS uses the
Supavisor pooler: `DATABASE_URL` on port **6543** (transaction mode) for the application,
`DIRECT_URL` on **5432** (session mode) for anything that must hold one connection.
`connection._engine_options` detects 6543 and sets psycopg's `prepare_threshold=None`,
because a prepared statement is named on the connection that prepared it and transaction
mode may hand the next transaction a different one — a failure that only appears under
load, after a query has run five times. `connection.assert_migration_safe` **refuses**
a migration over 6543 (Alembic's version lock is held by a connection, so pooling reduces
it to a formality), and `migrate_sqlite_to_postgres.py` refuses it too, for the different
reason that the copy is one long transaction. Pool sizing is `DB_POOL_SIZE` /
`DB_MAX_OVERFLOW` / `DB_POOL_RECYCLE_SECONDS`: a managed Postgres caps *connections*, so
idle ones are spent budget.

**Writable state must not live under `data/`.** `upload.files.upload_dir()` derives the
staging directory from `TRANSACTIONS_DIR`, and `data/` is mounted `:ro` to keep the source
workbook read-only — so the production compose points `TRANSACTIONS_DIR` and `REPORTS_DIR`
at `/app/var`, a named volume. Putting them back under `/app/data` fails every upload on a
read-only filesystem.

`scripts/migrate_sqlite_to_postgres.py` moves the existing warehouse across. It reads and
writes through the same SQLAlchemy `Table` objects in both directions, which is what makes
a JSON column arrive as `JSONB`, a SQLite `0`/`1` arrive as a boolean and an ISO string
arrive as a `timestamp` — the column types convert, not the script. It refuses unless both
databases are at the same head and **every target table is empty**, copies in
`sorted_tables` order inside **one transaction**, advances every identity sequence past the
largest copied key (without which the next upload collides with row 1), and re-counts both
sides, rolling the whole copy back on any disagreement. `test_supabase_migration.py` pins
all of it without needing a server.

## Architecture

### Request path (everything funnels through the same layers)

```
browser / WhatsApp / CLI
  → frontend/src/services/   the only place that calls fetch
  → backend/app/api/routes_*.py     thin; get_current_user + require_section deps
  → ai/tools.py                     34 typed tools, the sanctioned query surface
  → ai/permission_filter.py         role → data scope → filters injected into the SQL
  → ai/queries.py                   the warehouse access layer for this path
  → vw_* reporting views            all filter on is_void
```

Chat, `/api/dashboard/*` and `/api/pages/*` all take that path — `routes_dashboard.run()` wraps `execute_tool`, and `routes_pages.py` reuses it — which is why a page and the agent cannot disagree about a number. The exception is `/api/reports/*`, which queries the same views through `reporting/service.py` with `deps.enforce_report_scope` applying scope instead of `PermissionFilter`. Adding a report there means re-checking scope yourself; adding it as a tool gets it for free.

**Two surfaces reuse that path rather than opening a second one, and neither may become a way around it.** *Export* (`ai/export.py`, `POST /api/export`) renders only **already-validated** rows — either rows handed back from a chat answer or a stored assistant message the caller owns, re-read under their identity. There is deliberately no "export this query" path, so export cannot become an unscoped query surface; it truncates at `MAX_EXPORT_ROWS` and audits every call. *WhatsApp* (`app/integrations/`) is webhook → identify user by mapped phone number → permission check → the **same** agent the web UI uses, so a question asked in chat and over WhatsApp returns the same numbers under the same permissions. `NullWhatsAppProvider` (no credentials configured) records what it *would* have sent rather than pretending to send it — a dry run never gives a false impression of delivery.

The LLM (`ai/llm.py`, optional — `OPENAI_API_KEY` unset falls back to a deterministic planner that returns the same numbers) does exactly two things: pick one tool from an intent-restricted allow-list, and rephrase already-formatted prose. Intent, entity and date resolution are deterministic (`ai/intent.py`, `entity_resolver.py`, `date_resolver.py`). No SQL, no free-text field, and no unvalidated figure ever crosses the tool boundary; every tool schema is Pydantic with `extra="forbid"`, mandatory date bounds, and `limit` capped at 500.

### ETL

`etl/pipeline.py::run_import` is a 13-step orchestrator (stage → validate required → master codes → hierarchy → dates → numerics → duplicates → bulk upsert → persist rejections → complete batch) running as **one transaction**. New sources plug in at `etl/readers.py` by yielding `SourceRow`; nothing downstream changes — that seam is the point, and `POST /api/import/{data_type}/records` already exposes it. Rejected rows never reach a fact table; they go to `etl_rejected_records` with an error code and the full original row.

**An upload is a background job.** `POST /api/data-upload/preview` and `.../commit` stage the file, record the batch and answer **202**; `upload/jobs.py` runs the work on a bounded thread pool (one worker by default — SQLite has one writer, so a second would block invisibly inside the database). The Import Job ID *is* `upload_batches.upload_uuid`, surfaced as `job_id`; there is no second identifier. The worker owns its own `Session`, and a crash is recorded through a second clean one. A startup sweep in `main.py` fails any batch left `QUEUED`/`VALIDATING`/`IMPORTING` by a restart.

Because the run is one transaction, **progress cannot be reported through the database** — a row written inside it is invisible until commit, a dry run rolls it back, and on SQLite a second writer would block against the ETL's own write lock. `utils/progress.py` therefore holds live progress in process memory keyed by that same uuid, and the batch row keeps only the last `stage`/`progress_percent` so a cancelled or interrupted import can still say where it stopped. The pipeline emits phases through an optional `ProgressReporter`; one with no job is a no-op, which is what every scripted import gets. Anything that becomes a bind parameter must be chunked against `bulk.parameter_limit` (SQLite's ceiling is 32,766), and any *`async`* endpoint that calls into the pipeline must hand it to `run_in_threadpool` or it will hold the event loop for the whole import.

**Reading the file is most of an Excel import, which is why the read is the part that had to become measurable.** A 20,000-row workbook took 176 seconds, and 174 of them were spent before the pipeline reported a single percent. Layout detection was the cause: `master_data/inspector.py` asked whole-grid questions once per candidate — is this row blank, does this column hold anything, what are the labels on this line — and each answer rescanned every cell. That is quadratic in the row count, so a file four times larger cost sixteen times as much, and one real 20,879-row upload spent 210 seconds inside a single generator computing a cosmetic note about blank rows. **Every one of those questions is now asked once for the whole grid and answered by membership** (`_rows_with_content`, `_columns_with_content`, `_type_tallies`, and the bucketed `detect_header_row`/`detect_header_column`). The rule to keep: a helper that takes a grid *and one row* will be called once per row, and this module is where that becomes an hour.

**A workbook is streamed for the ETL and loaded whole for the master data, and the two entry points say which is which.** `inspect_sheet` opens the sheet in full and can therefore report its merged ranges and its declared dimensions; `inspect_streamed_sheet` works over a `read_only=True` worksheet and cannot. Streaming is what makes the read reportable at all — opened in full, openpyxl builds the entire sheet inside one call that offers nothing to observe, so two thirds of the wait passed with the bar unable to move. Streamed, the same work arrives a row at a time. It is also *faster* (6–8% measured, because no `Cell` object is built for the whole sheet), so the only cost is the merged ranges, which nothing on the ETL path reads — and `inspect_streamed_sheet` **says** they were not read rather than returning an empty list to be misread as "none". A file that declares no `<dimension>` gives no denominator and only the row counter moves; every file Excel itself exports declares one, and the ones that do not are generated.

**A validation reads the file once and a completed upload twice.** `_validate_transaction` used to build three readers — one for the headers, one for the preview, one for the pipeline — and each re-parsed the whole workbook; with the commit's own read that was four parses per upload. They share one reader now, which is safe because every reader caches its rows and hands out a fresh iterator, so the preview may stop at fifty rows without costing the pipeline anything. The remaining pair is irreducible: validation is a dry run that rolls its own work back, so the commit has nothing to inherit.

**Phase weights are measured, and one table cannot describe both formats.** `utils/progress.PHASE_WEIGHTS_BY_SOURCE` holds a `PhaseScale` per source type because reading is 55% of an Excel import and 5% of a delimited one. The numbers come from timing every phase over 5,000 / 20,000 / 100,000-row files of each format, where the shares hold within a couple of points across all three sizes; the old hand-set table gave MAPPING a quarter of the bar for 4% of the work. A **master upload has its own table** — it maps before it validates, the reverse of the ETL, and stages and writes nothing — and read against the transactional one its entire validation pass fell inside a band the bar had already passed. The order of each dict is the order of the bar, and each sums to 100; `test_upload_progress` pins that, pins the keys against `etl.readers.SOURCE_TYPE_*` (spelled by hand there because `readers` imports `progress`), and pins that the default is the *pessimistic* table so an unnamed source under-reports rather than claiming work it has not done.

**The percentage is never clamped, and that is deliberate.** A monotonic clamp was tried and removed: it did not stop the bar misbehaving, it hid three places where it already did — a master upload frozen for its whole validation pass, a reader's second pass frozen at the top of its band, and, worst, `jobs._mark_cancelled` filing a run stopped at 24% as 46%, which is audited and outlives the run. A bar that walks backwards is fixed by stopping the phases overlapping, not by papering over the symptom. So **a phase is announced by whoever does its work** — the reader owns READING and the pipeline claims it only via `progress.entered(...)` if the reader did not, which is what keeps that band alive for the in-memory `/api/import/*` source — and STAGING exists as its own phase because staging the rows and reading them are two waits that were sharing one band.

**A master-data upload is one SELECT and one insert, not two statements per row.** `upload/master_loader.load` preloads the dimension into a dict keyed the way the file keys its rows and writes new records through `etl.bulk.bulk_insert`; the per-row `flush()` went with the per-row SELECT it existed to serve. Loading 2,000 customers fell from 4,012 statements to 14. It is invisible on SQLite and it is the whole cost on a pooled Postgres, where each of those statements was a network round trip. Note that the ORM does **not** batch pending inserts even when they are flushed together — measured at the driver, 2,000 objects are 2,000 dispatches whether flushed once or per row — so the batching has to go through Core.

Facts reference dimension surrogate keys **and** keep the raw codes for the two `PENDING_SOURCE_DATA` dimensions (customer, sales force), which is what makes an unknown customer a deferred mapping rather than a rejection. Foreign keys reference business codes, not surrogate keys.

### Permission chain (four layers, any of which is final)

```
authenticated & active  →  role (section's role ceiling)  →  user section ALLOW/DENY
  →  data scope (region/area/territory… , hierarchical)  →  result
```

A lower layer can narrow access, never lift a restriction from above. Sections are declared once in `app/security/sections.py` and consumed by the `require_section(...)` dependency, `GET /api/admin/sections` and the frontend nav — adding a section is one entry. Scope is checked *before* the query runs and re-checked against the individual record on every write. Tokens carry identity only; role and scope are re-read from the database each request.

### The business map was removed (revision `0033_remove_map`) and rebuilt (`0034_business_map`)

`0033` removed the map entirely: the old `app/map/`, `routes_map.py`,
`models_map.py`, the marker library, the MapLibre renderer under
`frontend/src/components/map/` and the `public/geo/` basemap files, along with
eleven database tables and the `map` and `map_settings` sections. It is being
rebuilt from nothing, so nothing was kept "just in case": a half-removed
feature is worse than either state, and git holds the old one at `HEAD~2` of
the removal commit. What exists now is described below, in the order it was
built; `README.md`'s *Business Map (rebuilt)* section is the reference.

**Three things the map owned turned out not to be its own, and they stayed.**

*The organisational chain moved to `app/org/hierarchy.py`.* `ORG_CHAIN`,
`OrgScope`, `resolve_org_scope` and `resolve_business_entities` are the
Zone → Region → Area → Territory → Sub-Territory structure, which Data
Management scopes on (`datamgmt/scope.py`, `datamgmt/service.py`) exactly as
the map once did. It lived under `map/` only because the map was the first
surface to need it. Anything reaching for the hierarchy imports `app.org`.

*The four administrative dimensions stayed.* `dim_country`, `dim_division`,
`dim_district` and `dim_upazila` are master data — each has its own upload
template in `upload.registry`, its own Bangla name column and its own parent
link, and the Upload Centre offers them whether or not anything draws them.
Their **geometry** went: `map_area_boundaries`, `map_admin_points` and
`map_area_styles` existed only to be rendered, and are re-importable from the
published GADM/HDX release.

*The retired `MARKER_*` and `MAP_LOCATION_UPDATED` audit actions stayed* in
`models_ai.AuditAction`. Nothing writes them, but `audit_logs` still holds rows
carrying those values, and an audit trail that cannot name what it recorded is
not a trail.

**What the removal destroyed, deliberately and on instruction:** 1,397 entity
coordinates, 6,284 administrative points and 580 boundary rings. `0033`'s
docstring says so rather than leaving it to be discovered, and
`data/dev.db.pre0033.bak` is beside it.

When the map is rebuilt: `GROUP_BY_LEVEL`-style aggregation belongs behind the
tool layer like every other query, not in a second query surface, and the
renderer must not be the only thing that knows a marker's design.

**Step 1, schema and coordinates (`0034_business_map`).**
Four tables, not eleven. `map_entity_locations` is `0008`'s table column for
column, and `map_designs` / `map_layers` / `map_point_configurations` are
`0032`'s composition tables with `view_mode` added (Point / Boundary / Both per
layer, as the specification asks) and `marker_design_id` removed with the
library it referenced. The seed is one protected system-default design,
"Business Overview", with seven layers in this order: zone, region, area,
**unit**, territory, sub-territory, customer — Unit ships hidden but exists,
because the parent walk breaks without it; Customer ships hidden because 846
customer points at zoom 7 is the blue mass the old map removed multi-layer
drawing to avoid. **The seed carries no primary keys**: `0032` inserted
explicit ids, which leaves a PostgreSQL identity sequence at zero and makes the
first design an administrator creates collide with the seed. Everything the
map will read is in `app/map/`: `levels.py` (every drawable level, *derived*
from `org.hierarchy.org_level` plus customer and sales force — never a
hand-written list of tables) and `geo.py` (validate, centroid, bounds, upsert,
coverage, `derive_parents`, `restore_locations`).

**The coordinates came back from the export, not from a guess.** The 0033
removal exported the table to `reports/map_pre0033_20260903_092802/`, and
`scripts/reload_map_locations.py` restores only its *authoritative* rows (846
uploaded customers, 256 uploaded sales-force members) through the same
`validate` an upload passes, then recomputes every centroid above them. The
551 derived rows in the export are deliberately skipped: a centroid recomputed
from today's customer mapping is more honest than a snapshot. It is dry by
default and prints its target first, because `DATABASE_URL` decides whether it
writes `data/dev.db` or the PostgreSQL deployment. Both are loaded;
`data/dev.db.pre0034.bak` and `data/pg_pre0034-*.dump` are beside them.

**A centroid is written only for a code its master holds.** The deployment's
`dim_sales_force.territory_code` names sub-territory codes on 116 of 266 rows,
and the first derivation pass wrote 111 "territories" that were nothing of the
kind. `geo._write_centroids` now skips a parent code absent from the parent
level's master (logged, never re-filed at the level the code looks like), and
`derive_parents` first prunes any `DERIVED` row whose entity no longer exists.
Authoritative rows are never pruned: a coordinate a person placed is theirs to
remove. Coordinates arrive through the Upload Centre as the "Map Locations"
master type (`upload.registry.MAP_LOCATION_TYPE`, display group MARKET) whose
row check refuses an unknown level, an unknown code and null island, and whose
post-load hook re-derives the parents. `test_map_locations.py` pins all of it.

**Coordinates are also a Data Management entity, and the objection that kept
them out is what shaped how they got in.** They were excluded because that
screen "knows nothing about derivation" — true, and the wrong conclusion: the
Upload Centre was then the *only* way to reach a coordinate, so one could be
loaded and never afterwards seen, corrected or removed, and a bulk overwrite of
the whole file was the only edit available. Hiding the rows did not protect the
derivation; **routing the write through the map's own module does**.
`service._AFTER_MASTER_WRITE` is that route, and it mirrors
`upload.master_loader.POST_LOAD` on the same table so a file and a hand
correction leave the warehouse in the same state: an edited row becomes
`MANUAL` with its `derived_from` cleared (or the next derivation would
recompute the correction away, with nothing on screen to explain why), and
every level above is re-derived. The coordinate rules themselves live once, in
`geo.location_problems`, and both entry points read them — the upload adds a row
number and an error code, the form adds nothing. **It is the one master that is
removed rather than retired**: the row has no `is_deleted` (0034 recreated
0008's table column for column), nothing references a coordinate, and deleting
one leaves the customer or territory it pointed at untouched — the change log
keeps the whole record, and restore is refused with "create it again" rather
than pretending there is something to un-retire.

**Removing a coordinate is refused when the row is `DERIVED`, and the first
attempt at this screen got it wrong in the worst available way.** The delete
committed, the after-write hook re-derived, `_write_centroids` wrote a centroid
for every parent whose children are still placed — and the caller was told the
row was gone while looking at it. A derived row is not a record, it is a
**computation**, so `geo.removal_refusal` refuses it and names the lever that
does work. That refusal was itself a dead end until the other half landed:
`derive_parents` only ever *wrote*, so a centroid whose inputs had all been
removed survived forever, and being derived it could not be removed either.
`_prune_stale_centroids` is the companion to `_prune_orphaned_centroids` — one
catches an entity the master dropped, the other an entity that still exists with
nothing placed below it — and it runs **per level as the pass climbs**, before a
parent reads that level as its input, or a parent would be derived from a
centroid removed later in the same pass. Removing a *placed* coordinate from a
level with children below it succeeds and is **not** a failure, but it looks
like one, because the derivation immediately hands the entity its children's
centroid; `_after_location_write` returns a note saying exactly that and
`delete_master` puts it in the message.

Which of those a row is, is now **visible**: `source` and `derived_from` are on
the entity as read-only fields through `_SYSTEM_FIELDS_BY_TABLE`, even though
neither is an upload column — you cannot upload provenance, and without it the
two rows a reader most needs to tell apart are the same five columns. And
`master_row` publishes `_removable`, the server's answer **for that row rather
than for the entity**, so the table leaves the control off a row that would only
refuse: the same rule Target Management follows, for the same reason.

Three generic assumptions had to go for it, each a single-key assumption that
had never been challenged. **A master may be keyed on more than one column** —
`entity_type + entity_code` here, and `dim_plant`/`dim_storage_location` were
already composite and already silently broken by it: every lookup read
`key_fields[0]`, so `/api/master/dim_plant/C001` meant "some plant of that
company". `query.split_record_key` and `key_conditions` address the whole key,
joined by `KEY_SEPARATOR` (`|`, which is what `_record_key` always emitted and
what the browser sends back as `_key`). **A master may model no retirement**, so
the `is_deleted` predicate is asked for only when `entity.soft_delete`. And **a
page needs a total order**: ordering by the first key column alone is no order
at all when 846 rows share `customer`, and LIMIT/OFFSET over it repeats rows on
one page and drops them from another, so the remaining key columns are always
appended as the tie-break.

**`audit.sanitize` must return something the JSON encoder accepts, and the
reason is worse than tidiness.** Both its callers write the result into a JSON
column — `audit_logs.detail` and `data_change_log.old_values`/`new_values` — and
it used to pass unknown types straight through. A `Decimal` (what SQLAlchemy
returns for every NUMERIC column, and what the upload cleaner produces for a
`decimal` field) therefore raised inside `audit.record`'s flush, which is
wrapped in the `except` that exists so auditing cannot break a request — and
that handler calls **`session.rollback()`**, discarding the caller's own write
along with the audit entry. The route then committed a clean session and
answered **201 with the record in the body**, so creating the record silently
did nothing and reported success. This was live before the map locations work
and reachable from `dim_material.conversion_factor` / `transfer_price` and
`dim_upazila.latitude` / `longitude`; coordinates are just what made somebody
finally run it. `Decimal` now becomes `float` (matching
`queries.normalize_value`, so the change log and the reports state a figure the
same way), dates become ISO strings, and anything else unrecognised becomes its
`str` — closing the class rather than the two instances of it. Only *creates*
and *deletes* were exposed: `history.diff` already sent the update path through
`normalize_value`. `test_platform_api` pins the encoding and
`test_data_management` pins that a created record with a decimal field is
actually there afterwards.

**Step 2, the data (`app/map/metrics.py`, `app/map/data.py`, tool
`get_map_layer`).** The map has **one query and it is a tool**, registered in
`ai/tools.py` with **no intents** so the assistant is never offered it — a
question is answered by the ranked performance tools, and `test_map_data`
walks every `Intent` to prove the allow-list never contains it. It exists
because the map is the one reader for which "the top 500" is a wrong answer
rather than a long one: `queries.aggregate_by` caps at `MAX_ROWS`, and a
customer layer that silently omitted the 501st customer would draw a coverage
gap that is not there. So `queries.aggregate_every_group` runs the same
statement uncapped (`_grouped_statement` is now the one builder both readers
share), bounded by the master data rather than the facts, and
`MAP_SALES_MEASURES` adds a distinct-customer count to `SALES_MEASURES.sums`
*by reference* so the sales-report switch is still one switch. The tool joins
sales, targets and the comparison period's sales on the group code, the shape
of `target_vs_actual` without its ranking, and goes through `ctx.scoped` like
every other tool — a regional manager's map is their region, a user with no
scope is refused rather than shown an empty map, and asking for another region
by filter is a `PermissionDeniedError`. **Absent and zero stay apart on every
row**: an entity with a target and no sales rows sold nothing (`0.0`,
achievement `0`); an entity with sales and no target has `None` for target,
achievement and shortfall; an entity with nothing in the comparison window
has `None` growth, never −100%; volume is `None` where no line stated one.
`shortfall` is net sales minus target, negative when short — the executive
brand table's sign, deliberately not `queries.gap`. `metrics.METRICS` declares
the ten things a layer may draw and names the row column each reads;
`customer_count` is `unavailable_at=("customer",)` because a customer's
customer count is one. `data.layer_data` turns the rows into GeoJSON point
features (longitude first) through `geo.locations_for`, carrying each
entity's parent code from its own master row; **an entity with data and no
coordinate is reported in `unplaced`, never hidden**, the `(unassigned)`
group is handed back as figures rather than drawn, and a level above the
caller's data scope carries a note saying its figures cover only that scope —
the same partial number the Performance page shows at that level, now
labelled. `GROUP_BY_LEVEL` is derived from `levels.MAP_LEVELS`, and
`test_map_data` pins that the region layer equals `get_region_performance`
code for code.

**Step 3, what the map can draw (`security/sections.py`, `config.py`,
`map/basemaps.py`, `map/styles.py`, `GET /api/map/config`).** Two sections
came back: `map` is reporting, on by default for every role; `map_settings`
is a permission in its own right — off by default for everyone, on for
administrators, grantable to whoever owns how the map reads — and is what lets
somebody change what the map draws for everyone. Every read on
`/api/map/*` requires the first, every write an action on the second. **The
basemap is configuration, read once**: `MAP_STYLE_URL` / `MAP_STYLE_URL_DARK`
default to OpenFreeMap's positron and dark styles (no key, no billing, the
credit inside its TileJSON), and either may instead be a raster
`{z}/{x}/{y}` template — `basemaps.classify` tells the two apart, so the
browser wraps a template in the one-source style MapLibre needs, with
`MAP_GLYPHS_URL` for its labels, rather than being handed a URL it cannot
parse. A dark URL of a different kind than the light one is set aside with a
warning, not mixed; Satellite exists only when `MAP_STYLE_URL_SATELLITE` is
set, because a Satellite button that fails is worse than none; a design that
names a basemap the deployment no longer configures resolves to `standard`
with a note, never silently. `styles.py` declares the three colour modes once
— achievement bands at 90 / 70 / 50, diverging for a signed metric, quantile
class breaks for everything else — plus the neutral colour, the radius range
and the cluster paint, and `effective_style` merges a layer's `style_config`
over them **refusing any field it does not know**, because a saved setting
that changes nothing is a setting somebody will trust. Absent is drawn
neutral, never critical. `levels.MapLevel.boundary_source` is `None` on every
level (the masters carry no geometry and a division is not a sales region),
so `view_modes_for` offers Point alone; Boundary and Both appear the day a
level names a source, and are absent rather than inert until then.

**Step 4, composing (`map/designs.py`, `map/errors.py`, the design
endpoints).** Everything a design names is checked against the registries at
write time — a level, a metric *and* the level it is drawn at (a customer's
customer count is one), a view mode against what the level honours, a basemap
against the catalogue, a style override against `styles` — so a saved design
is never one the renderer would have to refuse; `InvalidLayer` names the
level and the field. A design inherits where it says nothing, and the payload
carries the effective value beside the stored one so the editor can show
which is which. Exactly one design is the default; when the default is
deactivated or deleted, `_hand_default_to_system` returns the flag to the
seeded design first, so the map never opens on a design that no longer
exists. The seed is protected from deletion and deactivation and from nothing
else — duplicate it and change the copy. Deleting a custom design is a real
delete: it is configuration about how figures are shown, not a figure, and the
audit trail keeps who removed it (`MAP_DESIGN_*` and `MAP_LAYERS_UPDATED`
actions, their own rather than `ADMIN_CHANGE`). Layers are matched by level on
replace so a surviving layer keeps its id. A refusal is a 409 carrying
`error_code` and the reason; an inactive design is a 404 to a reader who may
not compose.

**Step 5, drawing (`GET /api/map/data`, `GET /api/map/entities/{level}/
{code}`).** The data endpoint draws the requested layers of one design over
one period and filter set through `data.map_data`, adds each layer's
configuration, its extents and quantile `class_breaks` over the *placed*
features (the legend explains the points on the map), and a Top / Bottom
ranking over every entity with data, placed or not — `bottom` is the worst of
what `top` did not already show, and an entity whose figure is absent is not
ranked at all. A metric with no meaning at a level ranks that layer by its own
metric and says so. §70 of the specification is proved over HTTP against
`/api/pages/performance`: every report row is on the map with the same
figures, and the one extra row the map may carry is an entity with a target
and no sale, at zero, never at an invented figure. The entity endpoint returns
names only — the figures are the point's own properties — and is unscoped like
the filter options. Viewing is audited as `VIEW_REPORT` on resource `map`.

**Steps 6–8, the browser (`frontend/src/components/map/`,
`pages/BusinessMapPage.tsx`).** MapLibre GL JS 6.7 is ESM-only and locates its
worker with a runtime-built `new URL(…)` a bundler cannot see through, so
`useMapLibre` imports the worker with `?worker&url` and hands it to
`setWorkerUrl` — without that a production build 404s on the worker and draws
nothing. **One map instance for the life of the page**: filters and data
update sources and paint in place, a theme switch swaps the basemap with
`setStyle`, and the renderer is keyed on the map's style version so it re-adds
the business layers after every swap. `useLayerRenderer` is the one generic
renderer — one GeoJSON source and five MapLibre layers (clusters, cluster
counts, points, labels, selection ring) per business layer, stacked in the
design's order with labels on top, clustering only above the layer's
`cluster_at`, `fitBounds` once per data set and never on a toggle — and there
is no `if (level === …)` anywhere in it. `mapExpressions` turns the declared
style into MapLibre expressions and invents no threshold or colour. **The page
fetches one layer per request, in parallel** (`useMapLayers`): five layers in
one call waited 7.5 s on the PostgreSQL deployment before the first could
paint, and a toggle would have refetched all five. The reader's state is the
URL — `design`, `layers` (`layers=none` when every layer is off, because an
absent parameter means the design's own), `metric`, `selected=level:code` —
and Reset is one navigation; a composer's changes go through the Map Settings
drawer (`MapSettingsDrawer`, the `Modal` with `placement="sheet"`) and its
`DesignEditor` / `LayerEditor`, which send the whole ordered layer list so the
server validates the design as it will stand and show a refusal as the server
phrased it. The legend explains the *active* layer and offers the others by
name rather than stacking four legends over the basemap; the tooltip renders
the layer's configured `tooltip_fields`; the selected-entity card reads the
point's own figures and fetches only the ancestry; Top / Bottom are two
`DataTable`s (`map.top`, `map.bottom`). Tests stub MapLibre with a recorder
(`test/map.test.tsx`, `test/fakeMapLibre.ts`) — what they pin is what the
page asks the map to do, never WebGL. **Development `data/dev.db` holds no
sales rows**, so the map there is correctly empty; the localhost deployment's
PostgreSQL has the data, and it serves `frontend/dist` directly, so a frontend
change reaches it only after `cd frontend; npm run build` (no service restart
— nginx reads the bundle off disk).

**Step 9, the second tab (`map/locations.py`, `GET /api/map/locations`,
`0035_map_demarcation`, `0036_demarcation_all_levels`).** Area Demarcation
draws **the stated positions in `map_entity_locations`** and reads no fact table
at all, so nothing on it can disagree with a report — there is no figure on it
to disagree with. **A centroid is counted, never drawn**: a `DERIVED` row is
the average of the coordinates below it, so it marks a spot nobody surveyed, and
drawing it hollow told it apart while still putting a mark there. The exclusion
is server-side — filtering in the renderer would leave the counts describing one
set of points and the canvas another. So the acid test is a partition rather
than an equality: `drawn + derived == stored`, with each level satisfying
`available + derived + missing == total` (`missing` means *no coordinate at
all*, or an entity whose only coordinate is computed would read as unmapped).
That is stronger than the equality it replaced, because a row dropped for any
other reason still fails it. `0036` exists because the old equality failed at
1,124 of 1,139, the seeded design having hidden Unit and Sales Force and had no
layer at all above Zone.

**The two environments look nothing alike here, and it is the data.**
`data/dev.db`'s organisational coordinates are all centroids derived from its
846 uploaded customers, so every level above Customer draws nothing there; the
deployment uploaded its territories and sub-territories, so it draws 239 of 299
with 60 centroids withheld. A hidden level is one
fewer thing competing for the eye on a map of figures and a hidden **row** on a
map of coordinates. The design lives in the same tables behind
`map_designs.purpose`, so a reader is never offered the other map's design.

**A filter narrows by containment, not by row, and this is the one place the
platform departs from its own filter rule.** A report ANDs its filters and a row
matches only on a level it carries, so filtering by Region would drop every
coordinate row stating no region — the zone above and every customer below,
because a coordinate names one level and nothing else. Selecting a region on a
*map* means "this region and what is inside it", so the filter resolves to a
subtree: `resolve_org_scope`'s codes with everything above the deepest
selection trimmed off, that function returning ancestors *and* descendants.
`subtree_codes` says so at length because the flat rule is what the next reader
will reach for. The filter set is its own (`LOCATION_FILTERS` /
`location_filters`, both derived from `MAP_LEVELS`) with no material, batch or
period — a coordinate has none — and `queryFor(…, false)` drops the period, or
it would sit in the React Query key and refetch every coordinate for an
identical answer.

**Scope bounds what a reader is told exists, not only what they are shown.**
`available` is the "of how many" in "9 of 94" and it needs a *second*
containment — the scope alone — because read as the level's own row count it
handed a region-scoped manager the national figure as their denominator, a
total they may not see under a label calling it theirs. `total` and `missing`
are scoped for the same reason, and `missing` counts records with no coordinate
rather than records a filter excluded, so narrowing the map cannot manufacture
a data problem on the one screen whose job is saying what still needs
surveying. A filter outside scope is a 403 naming the code, never an empty map.

**`PermissionFilter.is_within_scope` is keyed on `region_code`, not `region`.**
`LEVEL_DEPTH` and `BINDING_BY_LEVEL` are both built from
`LevelBinding.code_field`, so passing the bare level makes `_ancestors` return
an empty chain and the check answers False for *every* code — a scoped reader
refused their own region, told "your access covers region REG001" in the same
sentence. The refusal test passed on it, having only ever asserted that a
refusal happened, which is why it now asserts the allowed case too. **A test
that only checks the negative passes on a function that refuses everything.**

**A level above the selection is empty by the rule, not by the data**, and
needs a different sentence: read through the counts it came out as "none of the
4 placed zone coordinates is inside region X", which describes the data and
sends somebody looking for a coordinate that is loaded and fine. That note is
suppressed when the reader did not filter — what emptied the level was then
their role, and "clear it to see this level" is advice they cannot take.

**Step 10, colouring by the parent (`styles.CATEGORICAL_PALETTE`,
`org.hierarchy.ancestor_codes`).** With the derived centroids set aside the map
is customer dots, and 846 undifferentiated dots do not show where one area ends.
The parent is read server-side — `ancestor_codes` is one pass of `_path_query`
for the whole chain, never a query per point — and it lives in `app/org` rather
than in the map because "which entity at level X contains this one" is an
organisational question. The server assigns every group's colour and the
renderer turns that list into one MapLibre `match` over `group_code`, wrapping
the existing `case` on `source`. That `case` is now inert on this tab, since
no `DERIVED` feature is sent at all, and it is kept rather than unwound because
the renderer is generic and the distinction costs nothing. Shape carries the
level and colour carries the parent: two questions about one dot, and collapsing
them into one channel would answer neither.

**Nothing cycles the palette.** Two neighbours sharing a colour on a map used to
judge where a boundary falls is a wrong answer, not an untidy one — so a level
with more groups than colours switches to *focus* mode (one group picked out,
the rest neutral, and **neutral until the reader picks**, because a focus nobody
asked for is a filter nobody applied). The threshold is the palette's own
length and the mode is decided from the groups **actually drawn**, so narrowing
the map can turn a level that could not be coloured honestly into one that
can. The
palette is thirteen because the deployment has thirteen regions and Region is
the level a reader reaches for first; eight of the thirteen are Okabe-Ito and
five are not, which is survivable only because colour is not the sole signal.
**Widening the palette is the only honest way to move that threshold** — raising
the cap without adding a colour would put two regions in one colour.

**Step 11, the administrative backdrop (`map/boundaries.py`,
`frontend/public/geo/`).** Divisions, districts and upazilas, drawn beneath the
points on both maps and **not business boundaries**: nothing states the outline
of a territory, so `boundary_source` stays `None` on every level and no outline
is ever coloured by a figure. They are static files rather than an endpoint,
and `boundaries.py` publishes the catalogue alone so the browser holds no list
of filenames. **The default is per surface** — `None` for the analysis map,
`upazila` for demarcation — because on a map of figures a backdrop is ink over
the subject and on a map for judging a line it *is* the subject; one constant
could not say both, so `catalogue()` publishes `defaults` keyed by purpose and
`boundary=none` is spelled out like `layers=none`. The five files came back
byte-identical from `4ee51b7`; `scripts/build_map_geojson.py` did **not**, and
cannot until `app.map.geometry` returns with it, so a new COD-AB release cannot
be processed today.

**Three traps this work walked into, none of which the suite would have
caught.** *An i18n key that already exists*: `map.colorBy` meant "Colour" on the
analysis legend, and adding the new control's label under it silently rewrote
that legend — check a key is free before writing it, not that it is present
after. *`window.location` under `MemoryRouter`*: the router keeps history in
memory and never touches the document, so every `expect(window.location.search)
.not.toContain(…)` passes against the empty string — `demarcation.test.tsx`
renders the router's own `useLocation().search` instead, and each absence is
proven against a value shown present first. *A stale name in `__all__`*:
renaming `DEFAULT_BOUNDARY` left the old name exported, which broke
`from … import *` and nothing else, because no importer here uses one. The
opening rule of this file applies to an export list as much as to a layer
catalogue.

**A backend change reaches that deployment only on a service restart, and this
sentence used to imply otherwise.** "No service restart" above is true of the
*frontend* and was read as covering both, which cost a round trip: a Map
Locations fix was reported as done, tested on the deployment and found still
broken, because `AIBusinessAgentAPI` runs uvicorn **deliberately without
`--reload`** (a file watcher would restart the process mid-import, which
`deploy/local/start-backend.ps1` explains) and so was still executing the code
it started with. Editing anything under `backend/` and rebuilding the bundle is
half a deployment. `Restart-Service AIBusinessAgentAPI` is the other half, it
**requires an elevated shell** so Claude Code cannot run it, and the person at
the keyboard has to. Check `upload_batches` for a `QUEUED`/`VALIDATING`/
`IMPORTING` row first — that is the import the missing `--reload` exists to
protect.

**And a restart can fail without failing.** nssm starts
`.venv\Scripts\python.exe`, which runs the real interpreter as a child, and it
is the child that holds port 8000; `AppKillProcessTree` is set, but nssm allows
each stop method only 1500 ms, so the parent dies first and the reparented child
outlives the tree walk. It keeps the socket, the restarted service cannot bind
and exits, and the orphan carries on answering with the code it started with —
four restarts in a row have "succeeded" this way while the API served hours-old
code, `Get-Service` reporting Running throughout. So the symptom has two causes,
the restart not run and the restart not taken, and
`deploy/local/restart-api.ps1` is what separates them: it ends whatever still
holds the port after the stop, then proves the process id changed.

**There is a third state, and `nssm.exe`'s own creation time is what names it.**
nssm *is* the service process, so a genuine stop-and-start gives it a new pid;
if `Get-CimInstance Win32_Service` names a process whose `CreationDate` has not
moved, the service was never stopped at all — which is neither "not taken" (a
*new* nssm with an old python squatting on the port) nor anything the script can
report, because the script never ran. A session spent four attempts and a
reported reboot in that state, with `Get-Service` saying Running throughout,
`LastBootUpTime` unchanged, and nssm, its child and the port holder all sharing
one creation timestamp. Check those three timestamps before believing any
restart, and note that the script writes
`deploy/local/run/logs/restart-api-*.log` as soon as it clears its two
pre-flight gates: **no transcript means it exited at the elevation check or
never started**, which is a different problem from a restart that failed.

Meanwhile the deployment can be verified without the service at all — load
`deploy/local/env.ps1`, `Import-ProductionEnv`, and call the module directly
against PostgreSQL. That answers "does the code work on the real data" while
"is the running service serving it" is still stuck, and the two questions are
worth keeping apart.

### Agent learning (`ai/feedback.py mining.py vocabulary.py lexicon.py`, migration 0025)

Intent classification reads hand-written keyword tables in `ai/intent.py` and entity resolution matches master-data names, and **neither guesses** — which is what makes an answer reproducible, and also why a word nobody has written down is a word the agent cannot understand. This subsystem is the way to write one down, and its whole surface is *interpretation*: which tool runs, which entity a word names, which period is meant. **Nothing learned ever reaches a number.** Aggregation, filtering, scope and permission are untouched, so an approved alias changes *which question gets answered* and never *what the answer is* (`test_agent_learning_guarantees.py` pins this, and that a learned alias cannot widen a user's scope).

Four tables, one migration, all additive. `agent_feedback` is one reader's verdict on one answer — the ground truth, because an answer that was confidently wrong looks identical to a right one until somebody says so. `agent_learning_signal` holds mined failures **deduplicated and counted**, because a reviewer's first question is "what fails most" and a hundred rows saying the same thing does not answer it. `agent_term_alias` and `agent_example` hold what a person approved.

**Nothing activates on its own, and that is the point.** A row is PROPOSED, a person approves it, and only then is it read; there is deliberately no propose-and-approve shortcut, and `CREATE` and `EDIT` are separate actions on the section so the two halves can be granted to different people. An agent that silently taught itself a wrong mapping would answer confidently and wrongly for every future question — strictly worse than the "not found" it says today. **An alias never outranks the master data**: `EntityResolver.candidates` consults it only after exact code, exact name and partial name have all come back empty, so this holds by construction rather than by convention, and an ambiguous term is still *refused* rather than resolved by the alias. **An alias may not point at something that does not exist** — an entity alias is checked against the master data and a metric/group-by/modifier alias against `intent.py`'s own tables, at propose time *and again* at approve time, because a record can be retired in between.

`lexicon.py` is the read path: only ACTIVE rows, cached on a **generation counter** bumped by every review write **after its commit** (before would let a concurrent question cache the uncommitted state under the new generation). Retiring is therefore immediate and reversible, and nothing is ever deleted. `intent.py` takes the learned keywords as an **overlay copied, never merged in place** — those tables are module-level and shared, so widening one would leak a deployment's vocabulary into every later call.

An approved example has two consumers and one rule. Without an LLM it is a deterministic shortcut; with one it is also a worked example in the planner prompt. Either way **its stored arguments are never replayed** — they carry one past caller's dates and filters, and the orchestrator builds its own from the validated query as always. An example may name the intent only where detection returned `UNKNOWN`, and may only choose a tool inside that intent's existing allow-list.

Mining runs inside `agent._persist` and never breaks a conversation: each write is in its own SAVEPOINT and the call is guarded, because an analytics side-effect must not cost a user their answer. A turn produces **at most one** signal — an unresolved entity also has an `UNKNOWN` intent, and recording both would double-count one failure and put the useless half in front of a reviewer. `ENTITY_NOT_FOUND` records the **term**, not the sentence; a scalar answer is not an empty one (`value` set, no rows), or every KPI answer would file a `ZERO_ROWS` signal and bury the queue.

### Target Management (`app/targetmgmt/`, `routes_target_mgmt.py`, migration 0027)

Every other target surface in this platform *reports* a target: `fact_target` holds what a file stated and `/api/pages/target` shows achievement against it. This package is where a target is **built** — and it is a separate section (`target_management`, off by default for every role) because seeing the number the sales force is measured on and deciding it are different privileges.

Eight tables, one migration, all additive. `fact_target` is untouched, so today's Target page and the agent keep seeing exactly one authoritative number throughout; **a version writes into `fact_target` only when it is locked**, which is also what freezes the figures that were agreed.

**Volume is the only figure entered, and the only figure stored.** A country target states a Target Volume per material; `quantity = volume / conversion_factor` and `value = quantity * transfer_price` are derived from `dim_material` at read time, the same reason `customer_name` lives on a view rather than on a fact. So a corrected transfer price corrects every unlocked report at once and cannot rewrite an approved one. **Never add a derived-quantity or derived-value column** to `target_country_line` or `target_allocation`: `fact_target.target_quantity`/`target_amount` are where a derived figure is frozen, and only at lock.

`conversion_factor` and `transfer_price` are the two columns revision 0027 added to `dim_material`, and both are **nullable with no default and no back-fill**. Neither can be derived from anything else this schema holds — a conversion factor is a property of the pack and a transfer price is a commercial decision — and a default of 1.0 would not read as "unknown", it would read as "one volume unit per saleable unit". A material missing either yields no quantity and no value and reports `n/a`; the 405 materials that predate the columns keep NULL until a Material Master file states otherwise. Both are declared in `upload.registry` as optional columns, so the template, validation, preview, edit form and export follow with no second list anywhere.

**One plan per scope, and the database says so.** `target_plan` is unique on (financial year, period, company, business unit, sales line) — a changed target is a new *version*, never a second plan nobody can tell apart from the first. **At most one version per plan is current**, and that is structural too: `target_version.current_plan_id` holds the plan's id while the version is current and NULL otherwise under a plain unique constraint, the `active_key` device 0025 introduced. NULLs are distinct in a unique constraint on both dialects, so superseded versions pile up freely while only one can claim the plan. Creating a version **copies** its predecessor's country lines rather than moving them, and leaves an approved predecessor's status alone — a version whose numbers left it is no longer the thing that was approved.

**Every allocation row is monthly, with no period-total row beside it.** A node's total for a period is a sum over its months and a level's total is a sum over its children, so a parent cannot disagree with its own parts because it is not stored separately from them. Reconciliation is therefore **computed, never stored**: a stored balance can go stale, and a stale one is worse than none because it looks authoritative.

`target_revision` keeps the system value, the requested value and the approved value as three columns, and `reason` is NOT NULL — "what the engine said" and "what we agreed" are different questions, and an unexplained revision is what the table exists to prevent. `target_approval` and `target_audit` are append-only and have no `updated_at`; a correction is a new row. `target_approval_matrix` is seeded by the migration with the default chain, because configuration with no rows is not a blank slate — it is a workflow in which nobody can approve anything.

**Two audit trails, deliberately.** `target_audit` is the business trail a planner reads on the Audit Trail screen (what a figure was, what it became, why); `auth.audit` is the security trail an administrator reads beside a login or a permission change. Neither answers the other's question, and `targetmgmt.audit.record` flushes rather than commits so the entry and the action it describes live or die in one transaction.

**`APPROVE` and `REVISE` are two actions, not one.** Signing off on a figure and asking for it to change are held by different people: a Sales Officer may revise their own sub-territory target and must never approve one, and `DEFAULT_ACTION_ROLES` names both explicitly because an action missing from that map defaults to *everyone*. The section holds no `DELETE` at all — a plan is superseded and a version is never removed.

**A total that cannot be completed is suppressed, not approximated.** `country.totals` returns `null` for quantity and value unless *every* line derived, and names the materials that stopped it. A partial sum labelled "Calculated Value" is not a small number, it is a wrong one — short by however much the underivable lines were worth — so the screen reads `n/a` and the response carries `notes` saying which materials to load and which column of the Material Master upload to load them in. The volume total is always real: it is what somebody typed. A per-cell `n/a` carries the same distinction, naming whether the Conversion Factor or the Transfer Price was the missing input.

**A volume is never coerced.** `country._coerce_volume` reads `12,500` and refuses `12,5OO` by name rather than reading it as 12 or as 0 — the no-invented-data invariant applied to one cell, and the reason the browser sends the raw text rather than a parsed number. Zero is accepted and meaningful; negative is refused, and with a different message, because “unreadable” and “negative” send a reader to different places.

**The review tree is the allocation's own snapshot, not the masters' current shape** (`targetmgmt/review.py`, `GET /versions/{id}/review`). Every allocation row names its parent, and the tree is read back through that chain — so a territory moved to another region next month cannot retroactively reshape a target somebody already approved, which is the whole reason `parent_code` is stored. The rows come back **flattened in reading order with a depth**, because the browser draws a table: expansion is a filter over the flat list, which is what keeps the review on `DataTable` and keeps the column arrangement every other report table has.

**Recon variance is zero at every level and is displayed anyway.** The engine distributes with largest remainder, so children cannot come to more or less than their parent — but a claim that is *checked and shown* is worth more than one that is merely true, and final approval is blocked while any level disagrees.

**Sales figures roll up the tree; target figures already do.** A sales row states a territory and a customer but **no sub-territory**, so the view's `sub_territory_code` is NULL on it — and reading each level directly would leave a sub-territory reporting no sales while its own customers reported plenty, on the one screen whose purpose is showing that things add up. So a parent's PY and actual figures are the sum of its children's, and only a leaf keeps what the view gave it. A branch with nothing recorded stays `None` rather than becoming zero, which is what makes growth read `n/a` rather than −100%.

**Scope narrows where the tree *starts*, never what the numbers inside it say.** A regional manager's tree is rooted at their region carrying that region's real figures. They never see a country row holding a narrowed total labelled “Country” — a figure that is neither the country's nor theirs — and never see the country's true total either. A reader whose scope names nothing in the allocation gets an explained empty view, not an error and not somebody else's data.

**`system_volume` and `current_volume` are the engine's suggestion and what stands.** They differ only at a node a management adjustment named: a node further down is split from its parent's *actual* figure and was never itself adjusted, so for it the two are equal and claiming otherwise would invent a suggestion nobody made. **Reconciliation reads `current_volume`** — it is about what the target *is*, and checking the pre-adjustment suggestion would report every adjusted allocation as broken.

**A limitation is never hidden, and never collapsed into a dash.** `targetmgmt/datastate.py` declares the five states a figure can be in and every readiness check, factor report and warning answers with one of them rather than a boolean: `VALID_ZERO` (the business sold nothing — a measurement, rendered `0`), `NO_DATA` (nothing loaded), `INSUFFICIENT_DATA` (loaded, but missing the column this needs), `INVALID_DATA` (present and self-contradictory), `NOT_AVAILABLE` (the platform holds no source at all — loading a file will not fix it) and `NOT_APPLICABLE`. A check's **data state and whether it blocks are separate questions**: a missing Conversion Factor is honestly `NO_DATA` and is *advisory*, because the engine allocates volume and volume needs neither derivation input.

**Nothing starts until the pre-flight passes** (`targetmgmt/readiness.py`, `GET /versions/{id}/readiness`). Nine checks — country target, conversion factor, transfer price, material master, sales history, hierarchy consistency, customer mapping, allocation rules, projected size — read only, cheap enough for every page load. Discovering that no customer carries a sub-territory *halfway through* a background job is the worst time to discover it, so the row-limit check in particular runs in the **request**, before the version moves and before a thread starts.

**The allocation level is reported, never assumed.** A Customer Master with no sub-territory mapping produces a tree that stops at sub-territory; that is a real allocation and is labelled as one, with the run ending `COMPLETED_WITH_WARNINGS` and saying so. It is never presented as a completed customer-level result. Reconciliation therefore checks **terminal** nodes rather than a level — a ragged hierarchy is the normal case.

**`target_allocation_max_rows` is a hard refusal, never a truncation.** Half an allocation reconciles against nothing, skips whichever customers sorted last, and looks exactly like a complete one. The refusal carries the whole projection (rows, ceiling, financial year, months, materials, nodes, level and the six dimensions to narrow by) and leaves the existing version untouched and usable.

**A management adjustment is node-level and absolute, never a percentage** (`targetmgmt/adjustments.py`, `target_adjustment`). A uniform percentage applied to every child and re-normalised is a mathematical no-op, so a global slider is worse than none — it appears to do something and cannot. An adjustment names a node and a signed volume, is funded **by its siblings pro rata** so the parent's total and the country target never move, and is refused rather than clamped when the siblings cannot fund it. It is an **input** stored against the version and re-applied on every run, so re-running after loading more sales keeps management's decisions; the figure is apportioned across the months in proportion to what each already holds, so a decision does not flatten a seasonal profile. `reason` is NOT NULL and every change is audited.

**Customer Potential and Territory Potential have no source and are declared anyway.** They report *Not Available — no potential master data configured*, are off by default and **cannot be switched on**. Deriving a potential from sales history would count one signal twice, under two names, since Historical Sales Contribution already reads exactly that data. The door is left open properly: `factors.POTENTIAL_MASTER_COLUMNS` records the shape a Potential Master would carry (customer code, potential volume, potential category, effective from/to, source, status), `PotentialSource` is the interface, and `set_potential_source` switches both factors on with **no allocation code change**.

**Every run is kept, failures included** (`jobs.history`). The question a planner brings to the list is usually “why did the last attempt not work”, so a run records its projected rows beside its generated rows, the level it reached, its warning count and how many sales rows it found — the last of which is **three-valued**, because a run that failed before reading never looked, which is not the same as looking and finding nothing. Retry is manual: nothing retries by itself, and a retry is a new run that leaves the previous version uncorrupted.

**The allocation engine allocates volume, and only volume** (`targetmgmt/engine.py`, with `factors.py`, `seasonality.py`, `rounding.py`, `reconcile.py` and `jobs.py` beside it). Quantity and Value are *derived from the allocated volume* afterwards, never allocated in their own right — a conversion factor differs per material, so a value allocated directly would imply volumes that do not add up, and the reconciliation the module exists to guarantee would be unreachable.

**Every split goes through largest-remainder distribution, so a parent equals the sum of its children by arithmetic rather than by hope.** `rounding.distribute` floors each share, counts the indivisible units left over and hands them to the largest fractional parts; the arithmetic is `Decimal` end to end and quantised to the `NUMERIC(18, 4)` the column holds. Rounding each share independently drifts in proportion to the child count — a thousand customers can lose whole units — which is why nothing here rounds any other way. Ties break deterministically (largest fraction, then largest weight, then lowest index) so the same plan generated twice is the same plan. `reconcile.check` then verifies it against **the stored rows** with zero tolerance, because “by construction” is a claim and a tolerance would hide the only bug worth catching.

**Reconciliation checks terminal nodes, not the deepest level.** A real hierarchy is ragged — one region reaches a customer while another stops at area because no unit exists under it — so volume that comes to rest at the bottom of a short branch is as final as volume that reaches a customer. Comparing a *level* total instead would report every ragged hierarchy as broken, which is most of them.

**An empty `fact_sales` produces no allocation, and says so.** The engine does not spread the country target evenly and call it a plan: `plan_allocation` returns `allocatable=False`, the job ends `NO_HISTORY` — its own status, not `FAILED`, because nothing went wrong — no rows are written, and the version returns to `DRAFT` so it stays workable. The screen reports the rows found, the years checked and which factors could not be calculated. **Seeded sales exist only in the test suite**; production waits for real data.

**The eight factors are declared once and configured, never hard-coded.** `factors.FACTORS` carries each factor's weight, default and data requirement, and the engine asks that module for a node's weight rather than computing one. They are not eight summands and the code does not pretend otherwise: five are `SHARE` factors that turn a node's children into a distribution and are mixed by weight, three are `MODE` factors that act on a different axis (which month), on what a node with no history is worth, or after the mixture. **Customer Potential and Territory Potential have no data source in this platform** — no master states a potential — so they are declared, off by default, and report why; deriving one from sales history would make the factor a second copy of Historical Sales Contribution. A **uniform** management adjustment is a no-op by construction (scale every child, re-normalise, get back what you started with), so it is held per node, which is the only form in which it means anything.

**Seasonality is measured, and its fallbacks are named.** Monthly weights come from the material's own monthly history, else its brand's, else the scope's, else an equal split — and whichever was used is reported as `MATERIAL` / `BRAND` / `SCOPE` / `EQUAL` with `is_fallback`. A twelfth each is the one shape the business is certain not to have, so it is never presented as a seasonal profile.

**An allocation is a background job, and its progress is durable** (`targetmgmt/jobs.py`, `target_allocation_job`). This is the opposite arrangement from the ETL and the difference is load-bearing: an allocation's expensive half is **pure reading**, holding no write lock, so progress can be written to the job row and read by a poll on another connection — it survives a page reload and a restart, where an in-flight upload's in-memory progress degrades to “no detail”. Only the final persist opens a write transaction, and by then the stage is already `FINALIZATION`. The job's status is **not** the version's: a failed job leaves its version where it was, and a version reaches `ALLOCATED` only once a job has completed *and* reconciled. A run that does not reconcile is rolled back rather than stored. `target_allocation_max_rows` is a **refusal**, never a truncation — half an allocation would reconcile against nothing and look complete.

**The allocation basis is read, never estimated** (`targetmgmt/history.py`). Two financial years of `fact_sales.volume` per material, out of `vw_sales_detail` through `queries.filter_conditions`, with the plan's company / business unit / sales line applied and the caller's own data scope merged on top by `PermissionFilter.enforce` — so a regional manager's basis is their region's history, and this figure and the Sales page cannot disagree. The measure is **volume**, because volume is what gets allocated; a basis in taka would be a different quantity from the thing it is a basis for. The basis years come from the plan's `basis_financial_years` where it states them and otherwise from the configured calendar, never from a hard-coded list.

**`SUM` over a nullable column understates silently, and this module refuses to.** A material with a hundred sales lines and ten blank volumes would report the other ninety as if they were all of it, so every year carries `rows` and `rows_without_volume` beside its total and any material with a blank volume is marked incomplete. The figure is still shown — it is the best that exists — but never as a complete history. Likewise **no history is not a history of zero**: a material with no sales in a year is `None`, growth against it is undefined rather than −100%, and its suggested basis reads `NO_HISTORY` instead of proposing a distribution built on nothing. `GROWTH_GUIDANCE_PERCENT` is declared once here and read by the allocation engine.

**A country line may only name a material of the plan's own company.** `dim_material.company_code` is the master's answer to which company a material belongs to, so a plan for one company refuses a material of another — and refuses one that names *no* company too, because putting a legacy NULL-company row into C001's plan would assert it belongs to C001, which is the guess the nullable column exists to avoid.

**A plan's scope narrows only on the levels a plan states** — company, business unit, sales line. A regional manager holds none of those, so the plan list is not hidden from them; their scope binds where their *targets* are read, which is the allocation tree. Same rule as the filter engine: a row only matches a filter on a level it actually carries.

### Revision and approval (`targetmgmt/matrix.py revisions.py approvals.py`, migration 0030)

**The approval chain runs bottom-up, and that is configuration rather than code.** ``target_approval_matrix`` says who signs at which level, in what order, and how far each may move a figure before it has to go higher; the *roles* are fixed but the workflow is each deployment's own decision. Sequence 1 is the sub-territory, where the person who has to hit the number sees it first, and the last step is Management — a target is *allocated* downwards and *reviewed* upwards, and the two directions are not the same journey. A **NULL sequence is outside the chain, never step zero**: the SFE / MIS administrator configures the run and signs off on nothing, and reading NULL as 0 would put them at the head of a workflow they have no business being in. A **NULL adjustment limit is unlimited and 0 is “may not change a figure at all”** — both are real settings, they look alike as an empty cell, and every read tests for `None` rather than falling back to a number.

**A revision is a request, and asking is not changing.** `APPROVE` and `REVISE` are separate actions on the section precisely so a Sales Officer can raise one against their own sub-territory and never sign one off. The row keeps the system volume, the requested volume and the approved volume as three columns, because “what the engine said” and “what we agreed” are different questions and a request that overwrote the first could answer neither; `reason` is NOT NULL.

**The adjustment limit routes a request rather than refusing it.** A change larger than the requester's limit becomes `ESCALATED` and names the role it went to — the *lowest* step whose limit covers it, not the top of the chain, because escalating a 12% change straight to Management when the zone manager's 15% covers it puts every routine correction in front of the person least able to judge it. `ESCALATED` is its own status rather than a flag on `PENDING`, or the limit would be unauditable: nobody could tell afterwards whether a request went the ordinary way or was routed past somebody.

**An approved revision is applied immediately and funded by the node's siblings**, worked one `(material, month)` slice at a time — for a fixed material and month the hierarchy is a plain tree of single numbers, and doing it on period totals instead would leave the monthly rows to be re-derived, which is where a unit goes missing. The change is apportioned across the node's slices in proportion to what each holds (so a decision does not flatten a seasonal profile), the siblings fund it pro rata through `adjustments.apply_to_siblings`, and every node whose figure moved has its subtree re-split in proportion to what it held. The country target does not move and the tree still reconciles exactly. A node with **no siblings is refused**: its parent's total is fixed all the way up to the figure somebody typed, so changing an only child means changing the country target, which is a different act with its own screen. The decision is also stored as a `target_adjustment`, so re-running the allocation after loading more sales does not silently discard a figure somebody signed.

**A node act and a version act are different rows, and `node_code IS NULL` is what separates them.** Granting somebody's revision is not signing off on the target: an approver who decided a request and was then told they had already approved the version would have had their signature taken without giving it. `target_revision.allocation_id` survives as the **anchor row** — lowest material, lowest month — and exists for its CASCADE, which is what makes a pending request vanish when the allocation it questioned is regenerated.

**A step approves only once every approving step beneath it has**, and the refusal names which one is outstanding — “still with the Area Manager” is actionable where “you cannot approve this” sends a reader to an administrator. **Final approval is gated on three things reported separately**: every step signed, reconciliation balanced at every level, and no revision still open. They are fixed in three different places, so one combined message would send a reader to the wrong one. A version that fails any of them stays `PARTIALLY_APPROVED`, which is a real state rather than a near-miss. **Approving is not locking**: an approved version can still be sent back until the lock, and only the lock writes into `fact_target`.

**A role absent from a matrix edit is left alone, never removed.** The screen sends what it changed, and `fields_present` names which optional fields the caller actually set — “leave the sequence alone” and “put this role outside the chain” both travel as `null` over JSON and mean opposite things. Deactivating is how a role leaves the chain, and it is reversible; there is no delete.

### Locking, and the business audit trail (`targetmgmt/lock.py`, `audit.trail`)

**Locking is the one act in this package that reaches outside it.** Every other module builds a proposal; this one writes into `fact_target`, which the Target page, every export and the AI agent read — so from the instant a version is locked, “the target” means one thing platform-wide. **Approval is a judgement and locking freezes figures**, deliberately two acts by the same role at two moments: until the lock an approved version can still be sent back and a corrected transfer price still corrects every report; after it the derived quantity and value are stored on the fact and a later price change cannot rewrite what was agreed. That is the whole reason `target_quantity` / `target_amount` live on `fact_target` and not on `target_country_line`.

**Five conditions, checked before anything is written and each reported by name**, because they are fixed in five different places. Approved. Balanced, and no revision open — both were gates on the last approval and both are **re-checked** rather than trusted, since an allocation can be re-run and a figure that no longer reconciles must not be frozen because it reconciled once. Every material carrying a conversion factor and a transfer price — **advisory for allocation and blocking for the lock**, which is not an inconsistency: the engine allocates *volume*, which needs neither, while a locked row states a quantity and an amount that cannot exist without them, and `target_amount` is NOT NULL so a missing input would have to be written as zero. And every terminal node reaching territory or below — not a rule invented here but `datasets.TARGET_ORG_LEVELS`, because a target *is* set for a territory and a Target file stating none is rejected by the ETL; a locked row is the same fact by a different route.

**Only terminal nodes are written**, and they sum to the country target exactly — which is what reconciliation proves, and why writing every level would multiply the target by the depth of the tree. The organisational codes come from each row's **stored parent chain**, never from re-walking the masters, for the reason the review tree gives. A branch that stops at sub-territory carries no customer rather than a guessed one.

**A re-locked plan restates its own rows and voids what it dropped.** The business key *is* the target's grain (`etl.datasets` — read from the spec, never restated, so a lock and a Target-file upload cannot key the same target two different ways), so V2 of a plan produces V1's keys and updates them in place: one authoritative number per grain. A key V1 wrote and V2 does not is **voided, never deleted** — the row, its provenance and its batch survive, and every reporting view filters `is_void`, so it leaves the reports in the same instant without leaving the record. `_existing_rows` is scoped to the plan's **own** batches: a target loaded from a file for the same grain is somebody else's row, and silently taking ownership of it would make a locked plan responsible for figures it never produced.

**One act, one trail entry.** The lock sets `locked_batch_id` *before* the status change so the single entry `plans.set_version_status` writes can name the batch; writing a second entry from `lock` put the same act in the trail twice, and a planner counting locks would have counted one too many. `audit.trail` is the read path — newest first, filtered and paged **on the server**, because a plan's trail grows without bound and shipping all of it so the browser could hide most of it would get slower exactly as the record gets more valuable. It returns the matching `total` beside the page, since a filter that found forty and one that found forty thousand must look different.

### Comparison and the overview (`targetmgmt/compare.py dashboard.py`)

**Only two versions of the same plan are compared.** Comparing V1 of one plan with V1 of another looks useful and is not: two plans have different scopes, materials and hierarchies, so a “delta” between them is arithmetic on two things that were never the same quantity. `CrossPlanComparison` names both plans rather than producing a table of apparent movements.

**A node in one version and not the other is `ADDED` or `REMOVED`, never zero.** A customer the newer allocation does not reach has not had its target cut to nothing — it has no target there, and the difference between those statements is the reason this platform distinguishes absent from zero throughout. Both its change and its change percentage are `null`, and the removed nodes are **appended rather than dropped**, because omitting them would under-report the change by exactly the volume that went missing. The tree takes the *newer* version's shape, since that is what somebody is about to act on.

**The headline total is computed from the tree's roots, never by summing the rows.** Summing a tree adds each figure once per level it appears at, which would report one movement several times over — the same reason the dashboard's allocated figure reads the root row rather than `SUM` over the allocation. Scope narrows where the comparison *starts*, delegated to `review._scope_roots` rather than reimplemented: two screens disagreeing about what a regional manager can see would be worse than either rule alone.

**The dashboard claims only counts of workflow state and figures somebody typed.** No scoped aggregate — one headline volume would mean something different to every reader and there would be no honest label for it — and no achievement percentage, because achievement against a locked target is the Target *page's* question and a dashboard that recomputed it is how two screens come to disagree. A plan with no country target reports `None`, not 0: nobody having typed a target and somebody having typed nothing are different statements. The four stages partition the plans, so a plan appears in exactly one and the counts add up.

**“Needs attention” is defined, never implied.** `ATTENTION_REASONS` declares each state and the sentence that explains it — allocated but never submitted, revisions still open, approved but not locked, last run failed — and the reason travels with the plan, so the screen never shows a red count a reader opens and cannot act on.

### Loading a country target from a file (`targetmgmt/bulk.py`)

**The upload is the same act as typing, not a second way in.** Every accepted row goes through `country.set_lines`, so a file cannot state something the grid would refuse and an uploaded figure is validated, audited and versioned by exactly the same code. The module validates first only so it can report every problem at once; it never writes a line itself.

**All-or-nothing, and every fault reported in one pass.** The ETL rejects individual rows and imports the rest, which is right for a transaction file where one bad invoice line does not change what the others say. A country target is not that — it is one deliberate list, and loading 297 of 300 leaves the target short by whatever the other three were worth, the same reason `country.totals` suppresses a partial sum. So a file with any unreadable row applies nothing, and the preview lists **all** of them: a file fixed one error at a time takes as many uploads as it has mistakes.

**A blank cell is “nothing stated here”, never a target of zero.** The template pre-fills every material of the plan's company, so most rows arrive blank; those rows are `SKIPPED` — counted, named in the notes, and left alone. Rejecting them would make the template unusable and writing them as zero would set a real target of nothing. The first version of the template also wrote its guidance into trailing rows, and `read` saw them as a material called “# One row per material”: a template its own reader refuses is worse than one carrying no instructions, so the CSV is data only and the notes live on the screen.

**It updates what it names and removes nothing.** Materials on the version and absent from the file keep their figures, and the count is reported so that promise is visibly kept. Deleting them would be a REPLACE, and this platform's upload modes are INSERT / UPDATE / UPSERT with no REPLACE — a file that was missing a sheet must not be able to empty a target somebody spent a week agreeing.

**Preview stages; apply re-reads and re-validates.** The preview is a photograph rather than a promise: a version can be approved, or a material retired, between the two calls. The token the preview returns is a **name**, resolved inside the upload directory and checked to be there, so `../` in a token reaches nothing. It runs **in the request** rather than as a job — a country target is one row per material, hundreds rather than the hundreds of thousands an allocation produces, so a background job would add a status to poll for something that finishes before the response does. Loading a target from a file is `UPLOAD`, not `EDIT`, so a planner who may adjust one figure by hand and one who may load three hundred at once can be different people.

### What the eleven Target Management screens agree on

The package is eleven tabs over one plan, and the things they have in common are load-bearing rather than incidental.

**Every table is a `DataTable` with a `tableId`** — `target-management.``{country,history,runs,review,compare,audit,upload-preview}` — so hiding a column, moving it and resizing it work on all seven exactly as they do everywhere else, and each remembers its own arrangement. A tree, a diff and an upload preview are all flattened server-side into rows with a depth for precisely this reason: a bespoke component would have lost all three.

**Tab, plan, version, material, comparison base, audit filter and audit page all live in the URL.** A link to “V1 against V3, Glyfon only” is shareable and the back button works, which is the rule filter state follows across the application.

**Scope narrows where a tree *starts*, never what the numbers inside it say**, and `review._scope_roots` / `_describe_scope` are the one implementation — `compare` delegates to them rather than restating them, because two screens disagreeing about what a regional manager can see would be worse than either rule alone. `_describe_scope` has **three** cases, not two: a restricted account with no scope at all sees nothing, and saying so plainly (and naming who grants one) beats the ungrammatical “Scoped to no data scope by your role” it used to produce.

**A figure that cannot be computed is `n/a` with a tooltip saying why, never a dash and never zero**, on every screen: growth against a year with no sales, achievement before the period has actuals, a quantity whose material states no conversion factor, a comparison against a node present on one side only, a country target nobody has typed, a blank cell in an uploaded file.

**A refusal names what to fix and where.** Blockers are listed one per line — approval, lock and readiness all follow this — because a missing transfer price is a Material Master upload, an unbalanced allocation is a re-run and a branch stopping above territory is customer mapping, and one merged sentence sends a reader to the wrong one.

**A control whose only outcome is a refusal is absent, not disabled.** The Revise column, the Apply button, the lock control and the matrix editor are all added to the page rather than rendered inert, because a button that never works teaches people to ignore buttons.

### Credit Control (`app/etl/credit.py`, migration 0031)

The warehouse half of the receivables module: one fact, one staging table, three views and the derivations behind them. See the reversal note near the top of this file for why receivables exist again at all.

**Stable derivations are stored; date-relative ones are never stored.** `net_invoice_amount` (invoice − return), `balance_amount` (net + payment + discount + adjustment — the signs are measured, see below) and `due_date_id` (stated, else invoice date + credit days) depend only on what the file said, so the ETL computes them once and the fact holds them. **`days_overdue`, `aging_bucket` and `credit_status` are not columns** — they depend on the day you ask, and a `days_overdue` frozen at upload is wrong the next morning while still looking authoritative, the same reason Target Management computes reconciliation rather than storing it. The views derive them from `due_date_id`, which is what lets `as_on_date` be a real request parameter instead of a label over stale figures. `test_credit_control` pins their absence, because the column is easy to re-add for a plausible-sounding reason (an index would be convenient) and every value in it would be wrong.

**One rule, written twice, pinned rather than trusted.** The bucket and status boundaries are frozen SQL in revision 0031 (a migration must keep producing the same schema forever) and live Python in `etl/credit.py` (the ETL goes on being edited). Two copies of one rule drift silently — the view bucketing an invoice one way and the loader another, each right in isolation — so `test_view_and_python_agree_on_every_boundary` walks every boundary day through both. Never edit one without the other; the test is what makes that safe rather than hopeful.

**Eight aging buckets, not the platform's six.** `NOT_YET_DUE · 1-30 · 31-60 · 61-90 · 91-120 · 121-180 · 181-365 · 365+`, an explicit decision: a 120-day debt and a 179-day debt are chased by different people. The specification wrote the last two as "181-365" and "365+", which claims 365 twice — the tail starts at **366** and the label stays cosmetic. `AGING_COLORS` in the frontend gains two entries to match.

**A cleared invoice ages nowhere, and a negative balance is cleared.** Aging measures money still owed, so `aging_bucket` is NULL once the balance reaches zero *or below*; that is what lets the aging chart and the outstanding KPI be counted against each other. Over-adjustment keeps its negative figure — never floored — carries `BALANCE_NEGATIVE`, and is excluded from the exposure total so a data-quality problem cannot net off against real debt and understate what is owed.

**Nothing is refused on an unverified assumption.** There is no CHECK on any amount and none on `credit_days`. A contradicting row is **flagged and kept** — `data_quality_flag`, pipe-separated because a row can be wrong twice and a reviewer needs both. That tolerance is what let the first real file load at all, and it is what made the two corrections below findable rather than fatal.

**The sign convention was measured, not assumed, and it is mixed.** The first real file settled what the specification could not: payment, discount and adjustment arrive **negative** and are *added*; return arrives negative too but is *subtracted*, because a returned-goods document offsets an invoice and increases what is owed on it. Agreement with the source's own Balance Amount over 16,614 rows, by rule:

| Rule | Agreement |
|---|---|
| `value − return + pay + disc + adj` (current) | 95.0% |
| `value + return + pay + disc + adj` | 93.5% |
| `net − pay − disc − adj` (as first built) | 54.2% |

The original all-subtract rule was wrong in the worst way: payment is *never* positive in this source, so subtracting it added it, and an invoice paid in full reported at roughly twice its value. Note the **adjustment sign is not determined by this data** — flipping it changes agreement by exactly nothing, because every row it would affect carries a zero adjustment. It is written `+` for consistency with payment and discount; that is a choice, not a measurement, and the first file with a non-zero adjustment that disagrees is the trigger to re-measure.

**A stated due date wins over a derived one**, which is also the reverse of how this started. The reasoning then was that a source computing a due date from terms it had not sent us should not move a reported figure. The file overruled it: 11,791 rows state `credit_days` of 0 beside a real due date, so deriving handed back the invoice date and made every one of them look immediately overdue. The stated date is the fact; the terms column is what is missing. Deriving is the fallback for a row stating no due date. `0` is therefore in `KNOWN_CREDIT_DAYS` — it is the ordinary value in this source, and flagging three quarters of a file teaches everyone to ignore the flag.

Both disagreements are still flagged. The figure no longer moves because of them, but "the terms do not explain this due date" and "the source's balance does not equal its own columns" are what a data-quality review needs to see. **A residual ~5% still disagree on balance** (23.7M BDT, 2% of outstanding) and is not yet explained; it is visible as `BALANCE_MISMATCH` rather than quietly absorbed.

**The deduction columns are stored signed and reported as magnitudes.** The sign is what makes the balance a plain sum, but a card headed "Total Payment" showing −1.10 Cr is not a figure anybody can read, and the payment rate under it would come out negative. `reporting.credit._deduction` and `queries.credit_totals` flip it once each, so the page and the assistant agree.

**An invoice number is unique within its company, not globally.** Two group companies each numbering from 1 is ordinary, and a global constraint would reject the whole of the second one's file. `vw_customer_credit_exposure` keeps company in its grain for the same reason — a customer trading with two companies has two sets of books, and collapsing them would leave a company-filtered report unable to answer for either. The customer join is a **LEFT JOIN on the code**, so an invoice whose customer the master lacks keeps its figures and shows its code rather than vanishing from a total.

**A bucket holding nothing produces no row.** `vw_credit_aging` reports what is there; rendering all eight in order and filling the absent ones with zero is the endpoint's job, because "no invoices in 91-120" is a fact the screen must state rather than a gap it should leave blank.

**Loading one (`etl/datasets.CREDIT_INVOICE`) needed three pipeline generalisations, and each was a single-dataset assumption that had simply never been challenged.** *A fact may now hold more than one date*: `date_field` still names the single reporting date and lands in `date_id`, but `DatasetSpec.date_columns` maps a field to its own column, and a credit invoice has four with no one of them being "the" date — you report on invoices raised in a period, but a row is overdue by reference to a different one. Every date named there is created in `dim_date` alongside the reporting date, or the foreign key fails at the write, after the whole file has been read. *A derivation may run before the fact is built*: `transforms.DERIVATIONS` is applied to the cleaned record, because the due date is an input to three later steps (the business key may name it, `dim_date` is built from it, the fact builder maps it) rather than an output of them — deriving it in the measure builder, the obvious place, would be after all three. *And a stock position is recognised by its storage location, not its plant.* The pipeline keyed `resolve_stock_masters` off `plant_id`; `fact_credit_invoice` names the plant that raised an invoice, so every credit row would have been rejected for stating no storage location and no material. The storage location is what actually distinguishes a stock position, and no other fact records one.

**Adding a dataset means updating the lists that do not derive.** The registries genuinely do — a `DatasetSpec` bought the template, validation, preview, ETL and rejection machinery with no further wiring. Three hand-written maps did not, and `upload.registry.display_group` **raises** on a key it does not know, so the upload centre broke outright until `_TRANSACTION_LABELS` and `GROUP_BY_KEY` were updated beside `STAGING_MODEL_BY_DATA_TYPE` / `FACT_MODEL_BY_DATA_TYPE`. This is the same rule the top of this file states about *removing* a dataset, in the other direction, and `test_etl_credit_invoice` now asserts it over `DATASETS` itself so the next one is covered on the day it is declared. `reporting/columns.py` and `datamgmt/catalogue.py` still list only three types, deliberately: those are the report layer and the Data Management page, and Credit Control reaches neither until its endpoints exist. Neither crashes meanwhile — both iterate their own list rather than `DATASETS` — so the dataset is simply not offered there yet.

**The Credit Control page draws the four filters the view can honour and no more** (`CREDIT_FILTERS`): company, plant, sub-territory, customer, plus the four credit-only narrowings. The sales hierarchy between company and sub-territory names columns `vw_credit_invoice_detail` lacks, and so do the three material levels — a credit invoice is money owed against a *document*, not against an item — so offering either would put a chip above unfiltered totals claiming otherwise. Same rule and same reason as `STOCK_FILTERS`. `credit_status` and `aging_bucket` are `STATIC_OPTIONS` because they are derived per reporting date rather than read from a master, and they travel with the As On date for that reason: the same invoice is Not Yet Due in June and Over Due in August.

**`AGING_COLORS` was replaced, not extended.** It held six keys — `CURRENT`, `91-180`, `180+` — and had held them since revision 0020 removed the only module that read them, so every key named a bucket nothing produced. The eight-bucket scheme shares not one of them. `creditControl.test.tsx` pins both halves: every bucket the backend can return has a colour, and none of the three stale keys survives.

**`Modal` gained a `placement`, and the behaviour did not change.** `sheet` docks the panel to the right edge so the invoice list stays readable beside the invoice being read; Escape, the focus trap, the scroll lock and the overlay click are shared with `center` because they are what a dialog owes the user wherever it is drawn. Below `sm` a sheet falls back to the centred layout — a phone has no room for a side panel.

**The outstanding trend is printed as a sentence, not drawn as a chart.** `_outstanding_trend` returns `NOT_AVAILABLE` with the reason, and the page renders that text. The source states one aggregate payment per invoice and one last payment date, so what was owed at a past month end is genuinely unrecorded; reconstructing it would mean assuming a payment pattern, and a chart of assumed history is indistinguishable on screen from a measured one. It becomes real when a payment-transaction extract exists — which is a different source, not a missing file.

**Scope on this view is enforced or the request is refused — never partially applied, on either surface.** `queries.filter_conditions` *skips* a filter naming a column the view lacks, and `vw_credit_invoice_detail` reaches the customer's sub-territory and no further, so a region-scoped caller's scope would be dropped in silence and answered with the whole company's receivables. `/api/reports/credit-control` returns 403 naming the levels it could not honour, and `tools._credit_scope_or_refuse` raises for the same reason — **if the tool path merely disclosed it, the assistant would be a documented route to figures the API declines to serve the same person**, which is exactly what a single tool layer exists to prevent. The stock tools meet the identical gap and only *disclose* it through `_note_unsupported`; that is right there and wrong here, because stock is deliberately not held below company while credit exposure decides whether a customer keeps getting supplied. The one deliberate exception is `get_business_alerts`, which **skips** the overdue check with a note rather than refusing: it answers four questions at once, and refusing all of them would deny a regional manager their stock and achievement alerts to protect a figure they were never going to be shown. The section defaults to the unrestricted roles, who have no scope to lose. This all becomes unnecessary the day the view carries the sales hierarchy above the customer — a `dim_customer → dim_sub_territory → …` join, and a new revision.

**There is a volume test, and it is opt-in.** `pytest -m volume` loads 50,000 credit invoices (`CREDIT_VOLUME_ROWS` sets the size) because three things break only at row count and pass every small test: **bind-parameter chunking** — `fact_credit_invoice` has ~30 columns against SQLite's 32,766 ceiling, so an unchunked insert fails somewhere above eleven hundred rows, which is to say on the first real file and never in a fixture; **the `due_date` index actually being used**, since a comparison that stops being index-friendly does not break, it just grows with the table; and **the aging buckets still summing to the outstanding total**, which is arithmetic nobody can get wrong across five invoices and a real boundary check across fifty thousand. `pytest.ini` carries `-m "not volume"` in `addopts`, so the ordinary gate is unchanged. Measured on this machine: 50,000 rows load in 8.6s and 200,000 in 37.3s (~5,400 rows/s, linear), each aggregate answers in 0.1s / 0.4s, and the last page of a 200,000-row paged read costs 1.0s against 0.64s for the first. `conftest_phase2.seed_master_data` was split out of the `seeded_engine` fixture so these can hold a module-scoped database seeded the same way as everybody else's rather than a second, drifting definition of it.

**`credit_days` is validated as a number and stored as an integer.** Numeric so an unreadable term is rejected with the same message as any other bad figure; `int` because the column is an INTEGER and SQLite will not bind a `Decimal` to one. The distinction the module draws is between *unexpected* and *impossible*: an unrecognised term is flagged and kept, a negative one is rejected, because it would derive a due date before the invoice existed.

### Frontend

React 19 + TS + Tailwind + Recharts, React Router. `services/` is the only code that calls `fetch`; `contexts/` holds Auth, Filter, I18n, Theme. **No business calculation and no financial-year logic lives in the browser** — charts and tables render rows the backend already grouped, which is why the dashboard and the AI assistant can never disagree about a number. Filter state lives in the URL so reports are shareable and drill-down works with the back button. Every user-visible string comes from `i18n/en.json` / `bn.json`.

**Every report table is one component, and how a reader arranges it is that component's business.** `tables/DataTable.tsx` draws every one of them, so hiding a column, moving it left or right and making it narrower or wider are written once and appear everywhere; a page adds none of it, and a page that adds a table gets all three for free. The arrangement lives in `hooks/useTableLayout.ts` and is remembered per table in `localStorage` under `bi.table-layout.<tableId>`, the way the language and theme preferences are — a table that passes no `tableId` still arranges and simply forgets on unmount, which is what an ad-hoc table wants (the AI assistant's answer table deliberately passes none: every answer has different columns, so there is no table for an id to name). Every page-level table otherwise passes one — a new report table without an id is an omission, not a decision. A page whose columns change with a selector folds that selection into the id — `master-data.${entityKey}`, `materials.${level}`, `sales.${tab}` — while `performance.breakdown` deliberately uses one id across every drill level, because those levels list the same columns and the arrangement should survive the drill. Three rules are load-bearing. A **stored order is reconciled, never trusted**: a column the report has dropped is removed, and — the half that matters — a column it has gained is inserted beside the column it was declared after, because an order that silently omitted a key would hide a column with no control anywhere to bring it back (`reconcileOrder`, pinned by `test/tableLayout.test.tsx`). A table **nobody has resized keeps the layout it always had**; the first drag measures every column, freezes it at the width the browser had already given it and only then switches the table to `table-layout: fixed`, so the switch moves nothing. And **reordering is not only a drag**: the ←/→ buttons in the column panel are what keep it reachable by keyboard and on a touch screen, so a change to the drag path must keep them working. None of this touches what was queried or what an export contains — an export still carries the backend's column order.

### Tests

Run against throwaway SQLite files, never PostgreSQL, and build their own workbooks in `tmp_path` — the real `data/Master Data.xlsx` is only ever read. `backend/tests/conftest.py` re-exports the phase-2 (`warehouse_engine`, `seeded_engine`, row factories) and phase-3 (`agent_engine`, `users`, `make_agent`) fixtures, so use those rather than building a database by hand. The SQLite engine fixture enables `PRAGMA foreign_keys=ON`; without it FK tests pass vacuously.

## Invariants — violating these breaks the product's core promise

- **No invented data, ever.** No master record, relationship, date or figure is guessed. An ambiguous date format is rejected, not interpreted; an unresolvable name is reported as not found; a validation failure aborts the import rather than being papered over.
- **Codes are strings.** `001` must never become `1`. `FieldKind.CODE` → `VARCHAR(64)`; normalisation is in `utils/text.py`.
- **Bangla/Unicode is preserved verbatim** — trim only, no case folding, no NFKC rewriting.
- **Nothing is deleted or replaced.** Master records are retired (`is_deleted`/`deleted_at`/`deleted_by`) and restorable; transactions are voided (`is_void`), never removed; upload modes are INSERT/UPDATE/UPSERT with **no REPLACE**; the only rollback is per-`import_batch_id`, transactional data, super-admin, audited. Business codes are read-only after creation.
- **The source workbook is read-only** — opened read-only in code, mounted `:ro` in docker-compose.
- **Suppress rather than guess.** A failed result validation (NaN, out-of-range percentage, row-count mismatch, wrong date window) suppresses the answer; a zero denominator renders `n/a`, never `0%`.
- **Financial year is configuration** (`FINANCIAL_YEAR_START_MONTH`, default 7 = July) and is never hard-coded anywhere outside `etl/calendar.py` / `config.py`.
- **Errors reveal nothing.** `api/deps.py` masks internal exceptions behind a generic message — no SQL, driver text or stack trace reaches the client; `/health` reports connectivity only.
- **Frontend permission controls are presentation, not security.** Every endpoint re-resolves permissions server-side; a hand-typed URL returns 403 and the refusal is audited.

## Conventions

- **`dim_material` is the one item master** (`0022_remove_product_architecture`). There is no `dim_product`, no SKU master, no Product Code and no separate sales brand: a sale, a target and a stock position all carry `material_code` and all resolve it against the same six-column master (code, description, group code/name, brand code/name). "Product", "SKU" and "item" all mean a row of `dim_material`. The 427 Product rows were exported to `reports/dim_product_pre0022.csv` and **none was migrated** — the Material Master is loaded from its own upload, and the historical facts took their Material Code from their own staging rows, which is the source file's identifier rather than the dimension's. Do not reintroduce a product table, a product-to-material bridge or a compatibility view.
- **The executive brand table reads each column from its own source table.** `get_material_brand_target_performance` (the dashboard's Top 15 Brands) takes Target Vol/BDT from `fact_target.target_volume`/`target_amount` and Sales Vol/Net Sales from `fact_sales.volume`/`net_sales`, each aggregated to brand *independently* and joined only on the brand name — never a raw-to-raw join, which would multiply a brand's rows. Achievement is actual ÷ target; **shortfall is actual − target** (negative when short), deliberately not `queries.gap`, which is the reverse. `get_material_brand_performance` stays the agent's cheaper brand tool and is unchanged. A target resolves to the 1st of its month, so a window excluding the 1st legitimately reports n/a achievement.
- **General performance ranks brands, not individual materials.** The dashboard, the sales page breakdown, the business-summary KPI and root-cause contributors all group by `GroupBy.MATERIAL_BRAND` via `get_material_brand_performance`; the generic product nouns in `ai/intent.py` map to brand, and only item-explicit nouns (`sku`, `material code`, `product code`, `product name`, `item`) reach `get_material_performance`. The Material Analysis page (`/api/pages/materials?level=material|material_brand|material_group`) is the deliberate exception and still ranks individual materials. Brand is an attribute of `dim_material` carried on every detail view — there is no Brand Master and must not be one.
- **A sales report states quantity, volume and net sales.** `gross_sales`, `gross_profit` and `gross_margin_percent` are not reported anywhere — no table, KPI, export or agent answer. They are still stored on `fact_sales`, still derived by the ETL, still in the views, and still editable in data management; only the reporting surface dropped them. The switch is `queries.SALES_MEASURES.sums`, which every sales aggregate flows through — change it there, never in a per-page column list. `test_brand_performance.py` guards this.
- **Sales Vol is `SUM(fact_sales.volume)` — the Total Volume the upload stated, never a calculation.** There is no volume unit, UOM or conversion factor in the sales path, and material stock has no volume at all: `etl.volume.from_source(volume, quantity)` stores the file's number and reads the quantity only for the negative-volume sign check. `volume` is therefore in `queries.SALES_MEASURES.sums` and comes back from `aggregate_by` like quantity and net sales; `queries.volume_total`/`volume_by_group` serve the unit-free readers. `fact_sales.volume_unit`/`volume_factor` survive as unwritten historical columns — no migration drops them.
- **No volume anywhere carries a unit** (`0022`). A target volume used to be the exception: a target named a SKU, so its unit was that SKU's `pack_unit` in the Product Master, and `queries.volume_by_unit`/`volume_totals`/`single_volume_by_group` partitioned on it to refuse adding a kilogram to a litre. Removing that master removed the unit those readers partitioned on — the Material Master states none — so all three are gone rather than left running on a column that would always be NULL and render every target volume as "mixed". `target_volume` is now in `queries.TARGET_MEASURES.sums` beside `target_amount`, and `volume_total`/`volume_by_group` take a `volume_column` and serve both sides. There is no `UNSPECIFIED_UNIT` and no MIXED basis left.
- Registries are **derived, never hand-written**: upload types come from `master_data.schema.TABLE_SPECS` and `etl.datasets.DATASETS`; the data-management catalogue comes from `app.upload.registry`. Add a column to a dimension and the template, validation, preview, edit form and export follow automatically — so extend the spec, don't add a parallel list.
- Migrations are `0001`–`0034` under `backend/app/database/migrations/versions/`. **Data is never truncated and no row is ever deleted**; new columns carry server defaults. A column may be dropped only after the data it held has been derived into its replacement in the same migration — 0013 and 0014 both do this on `fact_target`/`stg_target`, back-filling from each row's own `dim_date` entry before removing anything. Do not drop a column whose content cannot be reconstructed from what remains. When a migration rebuilds a view, base it on the body from the revision that **last authored** it (0009 added `WHERE is_void = FALSE` to the fact-reading views) — copying an older body silently resurrects voided rows in every report. `0016_material_stock` is the one exception to "nothing is deleted": it drops the old stock tables because the two models describe different things and neither can be derived from the other, so it **refuses to run while those tables hold rows** rather than discarding them. `0017_admin_layers` is purely additive — `dim_country`, `map_admin_points` and a country row in `map_area_styles` — and `dim_division.country_code` is deliberately **nullable** because divisions imported before any country file exist and the migration will not invent a parent for them. `0019_material_architecture` is the second exception: it drops `dim_material_location` because its rows carry no Material Brand and no Material Description, the two columns that now define a material, so a record derived from one would be incomplete — the rows were exported to `reports/` first and the migration **refuses to run while either stock table holds a row**. `0020_remove_receivables_and_warehouse` is the third: it drops `fact_collection`, `fact_outstanding`, their staging tables, `dim_warehouse`, the two warehouse columns on the sales tables and six views, and it counts every one of them first — a non-zero count anywhere aborts the whole revision rather than destroying part of it. On SQLite that revision also captures every surviving view, drops them and recreates them byte-for-byte around the column drop, because SQLite re-validates the entire schema during a table rewrite and a view two joins away fails just as loudly as a direct reader. `0022_remove_product_architecture` is the fourth: it drops `dim_product` outright after exporting it, because the replacement master is loaded from its own file and deriving one from the other is the exact thing the change exists to prevent. It back-fills `fact_sales`/`fact_target.material_code` from each fact's **own staging row** — the source file's identifier, never the dropped dimension — and counts four ways first (no staging row, ambiguous staging rows, a code the Material Master lacks, a pre-existing `materials` grant); a non-zero count anywhere aborts with the facts intact. It builds temporary indexes on `(source_file, source_row_number)` for that join and drops them afterwards: without them the guard is a nested scan over tens of billions of comparisons and the migration appears to hang. `0023_material_company` and `0024_customer_name_on_views` are both purely additive — one column plus two indexes, and a pair of view rebuilds respectively. `0025_agent_learning` is additive too: four new tables and nothing else touched. Its `active_key` column on the two approved tables is worth knowing about — it holds the natural key while a row is ACTIVE and NULL otherwise, under a plain unique constraint, because "at most one active meaning per phrase, and a retired one may be replaced" needs both halves and a partial unique index is PostgreSQL-only. NULLs are distinct in a unique constraint on both dialects, so the retired rows pile up freely while at most one active row can hold the key. `0026_remove_warehouse_marker` deletes one seeded marker design that outlived the entity type it named, and counts an assignment first. `0027_target_management` is additive: the eight Target Management tables, the seeded approval matrix, and `conversion_factor` / `transfer_price` on `dim_material` — both **nullable with no default and no back-fill**, because a conversion factor of 1.0 does not read as “unknown”, it reads as a claim about the goods. `0028_target_allocation_job` is additive too: one table holding one run of the allocation engine and how far it got. `0029_target_adjustments` adds `target_adjustment` plus four columns on the job row (`projected_rows`, `allocation_level`, `warning_count`, `sales_rows_found`), all additive. `0030_target_revision_node` adds four columns to `target_revision` — `version_id`, `level`, `node_code`, `material_code` — and corrects one piece of seeded configuration: `0027` seeded the approval chain **upside down**, Management at sequence 1 and the Sales Officer at 8, which taken literally means the CEO signs before the regional manager has looked. The correction runs in two passes through a negative holding value (the source and target sequences overlap) and applies **only where the row still holds the value 0027 wrote**, because a deployment whose administrator has already reordered the chain has expressed a decision that a migration fixing its own earlier defect has no business overwriting. `0031_credit_control` is purely additive — `stg_credit_invoice`, `fact_credit_invoice`, twelve indexes and three views — and touches no existing object, so the SQLite view-capture dance 0020 and 0022 needed does not apply. It is *not* built from 0020's `downgrade()`: that rebuilt a table holding a balance somebody else had calculated, and this one holds an invoice, so only the shape of `aging_bucket` carried over. Note that `alembic.ini`'s `sqlalchemy.url` is ignored — `migrations/env.py` resolves `os.getenv("DIRECT_URL") or get_settings().database_url`, and `database_url` is a **property** reading `DATABASE_URL` at access time — so a migration run with neither variable set does not fail, it silently succeeds against whatever `.env` names, which on a developer box is `data/dev.db`. Any test or script that migrates must set the variable *and assert the target*, which is what `test_credit_control._migrate` does. `0032_map_composition` added three map-composition tables and is kept as history rather than deleted — both databases were stamped at it, and a revision removed from the chain strands every database that has applied it. `0033_remove_map` reverses the whole map: eleven tables dropped in foreign-key order, plus the `map` and `map_settings` permission rows, which name sections the application no longer declares. It is the platform's most destructive revision — 1,397 coordinates, 6,284 admin points and 580 boundary rings, none of them recoverable from what remains — and unlike 0016, 0019 and 0020 it does **not** refuse to run on non-empty tables, because destroying those rows is the instruction it carries out rather than an accident it must prevent. The four administrative dimensions are deliberately untouched. `0034_business_map` is purely additive: the four rebuilt map tables and one seeded design, inserted **without explicit primary keys** so the PostgreSQL identity sequences advance. It loads no coordinates — a migration whose result depended on what `reports/` happened to contain would not be a migration — and `scripts/reload_map_locations.py` is the separate, dry-by-default step that restores them.
- **Stock is a dateless position with no volume** (`0016_material_stock`, `0019_material_architecture`). `fact_material_stock` references three masters and nothing else — `dim_plant` (Company + Plant, joined by `models.plant_key()`), `dim_storage_location` (Plant + Storage Location, `models.storage_location_key()`) and `dim_material` (keyed on `material_code` alone) — with no `date_id` and no organisational hierarchy below company. The source states neither, so no report may imply them. The four categories (unrestricted, quality inspection, blocked, in transit) stay four separate numbers because only unrestricted stock is sellable; `total_stock` sums all four including in transit and is defined **once**, in `vw_material_stock_detail`. Risk is measured by shelf life (`EXPIRED` / `EXPIRING_SOON` within `STOCK_EXPIRING_SOON_DAYS` / `VALID` / `NO_EXPIRY`), never by coverage days — that needs a rate of consumption, and a rate needs two readings and the time between them, which a dateless position cannot supply. (Stock and sales *do* share a Material Code since `0022`; it is the rate that cannot be derived, not the join.) A stock row naming a plant, storage location or material its master lacks is rejected, never auto-created, and each failure names the master to correct.
- **A stock figure is reported as `KG/LTR`, and the unit lives in the label.** A card reads `Unrestricted Stock (KG/LTR)` above `125,500`; a column heading carries the unit and its cells are plain grouped numbers; a chart passes the labelled name as its series (`CategoryBarChart`'s `valueLabel`) so the tooltip says it too; the agent writes `Unrestricted Stock (KG/LTR): 125,500` and never `125,500 KG/LTR`. The unit string is declared once on each side — `ai/queries.STOCK_UNIT` and `utils/format.STOCK_UNIT` — and applied through `stock_label`/`stockLabel` (name) and `format_stock`/`formatStock` (value); `humanizeColumn` adds it to a derived stock heading so a table that names no headers still gets the pairing. **Nothing is ever converted**: the uploaded value *is* the reported value, there is no factor in the source to convert with, and a kilogram is never derived from a litre or the reverse. There is no UOM column, unit column or KG/LTR selector anywhere in the stock surface, and adding one would mean inventing the per-row unit the source does not state. This is display only — `fact_material_stock` has no unit column and must not gain one.
- **A stock status colours the complete metric — its label and its value — and never one of them alone.** Green for unrestricted stock, the only stock that can be sold; amber for expiring-soon stock, a warning with time left to act on it; red for expired stock, money already lost. The three tokens are declared once as `.stock-status--unrestricted` / `--expiring` / `--expired` in `frontend/src/index.css` and looked up by metric key through `utils/format.stockStatusClass`, which answers to both spellings of each measure — a KPI or column key (`expired_stock`) and a shelf-life bucket code (`EXPIRED`). `KpiCard`, `StatCard`'s `statusKey` and `DataTable` (heading *and* cells) all read that one lookup; a component that spells its own emerald, amber or red has left the standard, and `charts/Charts.STOCK_STATUS_COLORS` is the one deliberate second copy, as fills, because an SVG bar takes a colour and not a class. Anything not in the lookup — a total, quality-inspection or blocked figure, any sales measure — stays neutral, and a card's supporting position count stays neutral under a coloured metric. Green and amber use the 700 weight in light mode rather than 600, because amber-600 on white clears the contrast floor for the large figure but not for the small label above it and the pair has to be legible as one unit; red is the 600 the rest of the application already uses. Colour is never the only signal — the label always names the status in words.
- **Material Group and Material Brand belong to the material, and identify nothing.** `dim_material` carries both as code/name pairs; a stock file states both and the ETL rejects the row when either disagrees with the master (`MATERIAL_GROUP_MISMATCH`, `MATERIAL_BRAND_MISMATCH`), which is why neither is in the position's business key — `material_code` already determines them. Since `0022` there is only one kind of brand: the sales brand it used to be contrasted with lived on `dim_product`, and that master is gone, so "brand" on a sales report and "brand" on a stock report are the same column of the same row. **A material brand is grouped and filtered by its name**, because `material_brand_code` is one constant value across the whole master and so distinguishes nothing; `GROUP_COLUMNS[MATERIAL_BRAND]` and the `material_brand` filter both use the name. A material group is grouped and filtered by its **code**, which is real.

- **A material belongs to one company, and the company is a column, not half of the key** (`0023_material_company`). The Material Master states Company Code *before* Material Code, and a report filtered to one company must not offer a material that company does not deal in. `material_code` stays `UNIQUE` — one row per material, company as an attribute. Keying the master on `(company_code, material_code)` was considered and rejected: every fact resolves its item by material code alone, and `fact_target` states no company of its own (it derives one from its territory, later in the pipeline than material resolution), so a composite key would leave a target unable to name an item until the org hierarchy had been walked. A Material Master naming the same material under two companies is a conflict **the upload reports**, not something the schema holds — choosing either would silently halve every report filtered to the loser. The column is **nullable only because of history**: the 405 materials loaded before it keep NULL rather than a guessed company, and **nothing back-fills it from `fact_material_stock`** even though that table states a company beside each material — that would make transaction data the authority for a master attribute, the exact inversion `0022` exists to prevent (and it would be wrong: 21 material codes appear under two or three companies in the stock data, because the same goods sit at several group companies' plants).

- **Customer is keyed on its code and labelled by its name** (`0024_customer_name_on_views`). `dim_customer` was `PENDING_SOURCE_DATA`, so the raw `customer_code` was both key and label and a customer-wise report showed `2000060120` where every other dimension showed a name. The master has arrived — 2,091 customers, and all 909 codes the sales data uses resolve — so `customer_name` now sits beside `customer_code` on the sales and target detail views exactly as `region_name` sits beside `region_code`. This changed **what is displayed, not what anything is keyed on**: the code stays the business key, stays what filters and scope apply to, and stays what a report groups by; nothing joins on the name and nothing may. The join is a **LEFT JOIN on the code** — left, so a sale whose customer the master lacks keeps its figures and shows its code rather than vanishing from a total; on the code rather than `customer_id`, because the surrogate key is NULL on any fact loaded before its master arrived. Retired customers are **not** excluded: `is_deleted` retires a record from selection, not from history.
- **A storage location is identified by `plant|location`, never by its code** (`0021_stock_filter_keys`). The code is unique only within a plant — `FG01` names 40 different places — so `vw_material_stock_detail` carries `storage_location_key` and `storage_location_label` (the name qualified by its plant), and `GROUP_COLUMNS[STORAGE_LOCATION]` and the storage-location filter both use the key. Grouping on the bare code merged separate locations that shared a name and split ones that did not.
- **The Material Stock page offers only the filters that view can honour**: Company → Plant → Storage Location for where a position is held, Material Group → Material Brand → Material for what it is, plus the derived shelf-life status. It draws no period — stock has no posting date — and none of the sales hierarchy below company, no customer, sales force or batch, because `fact_material_stock` has no such column. The three material levels are no longer what makes this set distinctive: since `0022` they are global and a sales page draws them too. `queries.filter_conditions` skips a filter a view lacks, so offering one would advertise a control that silently does nothing; `frontend/contexts/FilterContext.STOCK_FILTERS` is the set, `queryFor(levels)` keeps another page's filter out of the request, and the bar's chips and "clear all" describe only the levels the page manages. Group → Brand is a narrowing, not a hierarchy: a brand can sit under more than one group, so a brand implies no group. Every stock tool calls `_note_unsupported`, so a filter reaching the agent or a hand-typed query string is still disclosed rather than ignored in silence.
- **A target is a month of a financial year, not a date** (`0014_target_structure`). `fact_target` is ten columns — target month, financial year, territory code, sub-territory code, customer code, material code, sales force code, and the three measures — with every master attribute reached through the code that references it, so renaming a customer updates every report at once. `target_period`/`target_type` were replaced by `target_month` + `financial_year` so the upload can validate the pair against the configured calendar rather than accept any label an operator types. The Target file's column is still headed `SKU Code` — that is what the planners call it — and the dataset spec accepts either heading; what it resolves against is the Material Master.
- **A target carries amount, quantity and volume**; only `target_amount` is required, and NULL means "no target set for this measure", never zero. The volume column keeps the `target_` prefix because a planned volume has no invoice line behind it to state a total on. There is no `target_volume_unit`, on the fact or on the view: see the no-unit convention above.
- **The filter hierarchy is bidirectional, and declared once.** `routes_masterdata.FILTER_PARENTS` is the single map of level → parent: the nine organisational levels are *derived* from `etl.mapping.LEVEL_BINDINGS` (never restated — a second copy could drift from the one the warehouse is built on, and note it is nine, with `bu_code` between company and sales line), plus four declared edges that each name a real column — `customer_code`→`dim_customer.sub_territory_code`, `sales_force_code`→`dim_sales_force.territory_code` (inert; that dimension holds no rows), `material_code`→`material_brand`→`material_group_code` on `dim_material`, and `storage_location_key`→`plant_code`→`company_code`. Since `0022` the material chain is **global, not stock-only**: it narrows a sales or target report exactly as it narrows a stock one, and `sku_code`/`brand`/`category` are gone from `FILTER_PARENTS`, `INDEPENDENT_FILTERS` and the options endpoint. The plant chain and the shelf-life bucket are what remain stock-only. **Child → parent** is `resolve_ancestors`, served by `GET /api/master-data/ancestors/{level}` in **one** request for the whole chain and applied by the browser in **one** `setSearchParams`. Resolution is only ever started by a user changing a control — nothing watches filter state and re-derives from it — which is what makes a loop impossible by construction. A parent is selected **only when the master states it unambiguously**: a material brand that appears under two material groups resolves to no group rather than to one of them, because choosing either would silently narrow the report to half the brand.

- **A filter's options come from the whole selection, not from its parent** (`api/filter_space.py`). Asking "what sits under my nearest selected ancestor?" has a wrong answer whenever that ancestor is not the *immediate* parent: choosing a company and opening Territory asked `dim_territory` for rows whose `unit_code` equalled a company code, which is nothing at all — 0 options against that company's real 154. So the module asks one different question for every control at once: **given everything currently selected, which values does the master data still allow here?** A level is never gated on its parent being chosen, only on a consistent value existing, which is what makes Company + Territory, Company + Sub-Territory and Territory-then-Company all work. There are **three independent spaces** — org (the nine levels plus customer and sales force), plant (Company → Plant → Storage Location), and material (Company → Material Group → Material Brand → Material) — and `company_code` is the one level all three share, so it is the only channel by which they constrain each other. That propagation is deliberately **one hop through the shared company**, never a join across spaces: no master relates a territory to a plant, and inventing one would be the guess this system exists to avoid. Two rules keep it honest: **a filter never restricts itself** (choosing three territories must not make the other 151 unreachable, so a level's options are computed from every *other* selected level), and **an unconstrained space contributes nothing** — a space with no selection of its own does not narrow the shared company, and a space whose matching rows record no company says "unknown" and is skipped rather than emptying every other control. A row only matches a filter on a level it actually carries. `test_filter_space.py` guards this.
- **Changing a filter cascades; clearing one does not.** Pointing a level at a different value invalidates everything beneath it (an area under a different zone must not survive), but removing a level leaves its children alone — a child is still a valid narrowing on its own, filters are ANDed server-side, and a customer already implies its territory. This is what lets a user take off a parent that was auto-selected from a child without losing the child they chose, and without it snapping back. `FilterContext.autoSelected` records which levels resolution filled in, deliberately **not** in the URL: it explains how a selection came about, and a shared link should reproduce the filters, not the story behind them.
- **Company is drawn in the filter bar's always-visible row, and in no group.** `FilterContext.GLOBAL_FILTERS` names the levels the bar renders beside the period rather than inside its collapsible panel; `company_code` is the only one, because it heads both `SALES_CASCADE` and `PLANT_CASCADE` and so narrows sales, target and stock at once — a filter with that reach should not need a panel opened to see. This is **layout only**: Company stays an ordinary member of both cascades, of `ALL_FILTERS`, of `STOCK_FILTERS` and of whatever set a page manages, so its chip, its share of the active count, "clear all", the cascade it triggers and the parameter it contributes are unchanged. `GlobalFilterBar` unions `GLOBAL_FILTERS` into `managed` and filters it *out* of both the grouped and the flat grid — that pair is what stops Company appearing twice, so a level added here must be removed from every group list at the same time (`DASHBOARD_FILTER_GROUPS` no longer starts its sales run with company). Period comes first and Company second in one `flex-wrap` container, which is what keeps that order when the row wraps on a narrow screen; neither carries a responsive hiding class.
- Router registration order in `api/__init__.py` is load-bearing (auth first; `data_management_router`'s `/api/master/{entity}` after the more specific `/api/master-data/*`).
- Code comments here explain *why*, often citing the decision that forced the choice. Match that; a comment restating the code is noise.
- Settings are read once through `config.get_settings()` (an `lru_cache`d frozen dataclass with a minimal `.env` loader) — don't call `os.getenv` in feature code.
