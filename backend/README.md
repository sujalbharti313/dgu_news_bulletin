# AI//CAMPUS backend (Phases 2, 3.1 and 3.2)

A public, read-only REST API for the AI//CAMPUS bulletin, backed by Supabase PostgreSQL, plus (Phase 3.1)
automated news ingestion from RSS/Atom feeds and the TLDR AI newsletter via Gmail (Phase 3.1), and news
processing, deduplication and story clustering (Phase 3.2). See [News ingestion](#news-ingestion-phase-31) and
[News processing](#news-processing-and-deduplication-phase-32).
FastAPI · Pydantic v2 · SQLAlchemy 2 · psycopg 3 · Alembic · httpx · feedparser · pytest.

The backend is independent of the Next.js frontend: it lives in `backend/`, has its own dependencies,
and deploys to any Python host. The frontend talks to it only through `NEXT_PUBLIC_API_BASE_URL`.

> **Status of the live database:** the Supabase project has migrations `0001` and `0002` and the reference data.
> Migration `0003` (Phase 3.2 processing columns) has been tested against a local PostgreSQL 16 only; apply it
> with `alembic upgrade head` before running processing against Supabase.

---

## Architecture

```
backend/
├── app/
│   ├── main.py               App factory: middleware, routers, error handlers. `uvicorn app.main:app`
│   ├── core/
│   │   ├── config.py         All settings, validated (pydantic-settings). DATABASE_URL normalisation
│   │   ├── errors.py         ApiError types + handlers → {"error": {code, message, details}}
│   │   └── logging.py        Plain or JSON logs; no credentials or query strings
│   ├── db/
│   │   ├── base.py           Declarative base, constraint naming, UUID/timestamp mixins, CHECK helpers
│   │   └── session.py        Lazy engine, pooling, per-transaction statement timeout, sessions
│   ├── models/               SQLAlchemy tables (one module per entity) + enums.py vocabularies
│   ├── schemas/              Pydantic response/query contracts (camelCase JSON = src/lib/types.ts)
│   ├── repositories/         Queries. visibility.py holds every publication rule in one place
│   ├── services/             Use cases, row→contract presenters, URL canonicalisation
│   ├── middleware/           Request id + access log + security headers; per-IP rate limiting
│   ├── api/v1/               Versioned router and endpoint modules (admin_ingestion.py: token-protected)
│   ├── ingestion/            Phase 3.1: sources, SSRF guard, HTTP client, providers, parsers, store, runner
│   ├── processing/           Phase 3.2: normalisation, deduplication, story clusters, Stage 3.3 contract
│   └── seed/                 Seed runner + JSON fixtures exported from the frontend's data files
├── config/                   ingestion_sources.toml (the feeds and newsletters to collect)
├── migrations/               Alembic environment and versioned migrations
├── scripts/                  seed.py, ingest.py, process.py, gmail_oauth.py CLIs, export_frontend_fixtures.mjs
├── tests/                    pytest suite (API, constraints, migrations, contract, seed, config)
├── alembic.ini · pyproject.toml · requirements*.txt · Dockerfile · .env.example
```

Request flow: endpoint (validation, status codes) → service (use case, mapping) → repository (SQL) → model.
Endpoints never build SQL and repositories never build HTTP responses.

## Prerequisites

- Python 3.12+ (3.12 is what the Dockerfile and the test run used).
- A Supabase project, or any PostgreSQL 14+ for local work. `gen_random_uuid()` is built in from PG 13.
- For tests only: a local PostgreSQL you can create throwaway databases in.

## Local setup

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # runtime + pytest/httpx/ruff, all versions pinned
cp .env.example .env                 # then set DATABASE_URL (see below)
```

`requirements.txt` is the fully pinned runtime lock (compiled from `requirements.in` with
`uv pip compile --universal --python-version 3.12`, so Windows, macOS and Linux all install from it); `requirements-dev.txt` adds the test tools. To upgrade, edit the `.in` file and recompile.

## Environment variables

All settings are read from the environment or `backend/.env` (run commands from `backend/`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | `development`, `test`, `staging`, `production`. Production enforces stricter rules. |
| `DATABASE_URL` | unset | Supabase Postgres URL. `postgres://`, `postgresql://`, `postgresql+psycopg://` accepted. Required in production. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | SQLAlchemy pool per worker. |
| `DB_POOL_TIMEOUT_SECONDS` | `10` | Wait for a pooled connection before failing with 503. |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | TCP/TLS connect timeout. |
| `DB_STATEMENT_TIMEOUT_MS` | `10000` | Per-transaction `statement_timeout` (`set_config(..., true)`, pooler-safe). `0` disables. |
| `DB_PREPARED_STATEMENTS` | auto | Auto-disabled on port 6543 (transaction pooler). |
| `DB_USE_NULL_POOL` | `false` | Let Supabase's pooler own connections. Use `true` with the transaction pooler. |
| `CORS_ORIGINS` | localhost:3000 | Comma-separated exact origins. Wildcards are rejected at startup. |
| `EXPOSE_DEMO_CONTENT` | `false` | Serve `is_demo` rows. Rejected when `APP_ENV=production`. |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per client IP, per worker. `0` disables. |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | Logging. |
| `DOCS_ENABLED` | `true` | Serve `/docs`, `/redoc`, `/openapi.json`. |
| `TEST_DATABASE_URL` | unset | Tests only. Must be a local database whose name contains `test`. |
| `INGESTION_*`, `GMAIL_*` | see below | Phase 3.1 ingestion. See [Ingestion settings](#ingestion-settings). |
| `PROCESSING_*` | see below | Phase 3.2 thresholds. See [Similarity thresholds](#similarity-thresholds). |

Never put any of these in the frontend. The only frontend variable is `NEXT_PUBLIC_API_BASE_URL`.

## Supabase setup

1. **Create the project** at supabase.com → New project. Choose a region close to your users and
   backend host (Mumbai `ap-south-1` for Dehradun). Save the database password in a password manager.
2. **Get the connection string**: dashboard → **Connect** (top bar) → *Connection string* → URI.
   Choose the mode that fits where the code runs:

   | Mode | Host and port | Use it for |
   | --- | --- | --- |
   | Direct | `db.<ref>.supabase.co:5432` | Long-running servers **with IPv6**. Best for migrations. |
   | Session pooler | `aws-0-<region>.pooler.supabase.com:5432` | Long-running servers on **IPv4-only** hosts (most PaaS), and migrations from IPv4 machines. |
   | Transaction pooler | `aws-0-<region>.pooler.supabase.com:6543` | Serverless or many short-lived instances. Set `DB_USE_NULL_POOL=true`. |

   The user name differs between modes (`postgres` for direct, `postgres.<ref>` for poolers); copy it
   from the dashboard rather than editing by hand. URL-encode special characters in the password.
3. **Store it securely**: put it in `backend/.env` locally (git-ignored) and in your host's secret
   settings in production. Do not paste it into chat, issues, source files or the frontend.
4. **Apply the migrations** (next section), from a machine that can reach the database.
5. **Optional, development projects only**: seed demo data. Never seed demo data into production.
6. **Check the Data API exposure**: the migration enables row-level security on every table with no
   policies, so Supabase's `anon` and `authenticated` keys cannot read or write these tables through
   the auto-generated REST API. The backend connects as the table owner (`postgres`), which bypasses RLS.
   If you later run the app as a different, restricted role, grant it `SELECT` and either make it the
   owner or add explicit RLS policies for it. Do not disable RLS.

## Migrations

```bash
cd backend
alembic upgrade head              # apply everything (prints the target host, never the password)
alembic current                   # show the applied revision
alembic history                   # list revisions
alembic upgrade head --sql        # print the SQL without running it (review before production)
```

- Each migration runs in one transaction; a failure leaves the database unchanged.
- Migrations **never** run on application startup. Run them as a release step before deploying new code.
- For a schema change: edit the models, then `alembic revision --autogenerate -m "describe change"`,
  review the file by hand (autogenerate misses some things, such as triggers), commit it, and apply.
- `alembic downgrade` drops tables. It exists for local development. Do not run it against a
  database holding real content; ship a new forward migration instead.
- Prefer the direct or session-pooler URL for migrations. The transaction pooler also works because
  prepared statements are disabled on port 6543.

Initial schema (`0001`): `sources`, `editions`, `stories`, `audio_briefings`, `tools`, `research_papers`,
`india_entries`, `career_signals`, `campus_items`, `ingestion_jobs`, plus an `updated_at` trigger on each.
Ingestion (`0002`, additive): the temporary `ingestion_items` staging table, and `source_key`, `details`, a
`skipped` status and a one-running-job-per-source index on `ingestion_jobs`. See [Database changes](#database-changes-migration-0002).
Processing (`0003`, additive): six processing columns on `ingestion_items` and the `story_processing` job type.
See [Storage policy and database changes](#storage-policy-and-database-changes-migration-0003).

## Seeding

```bash
python -m scripts.seed --reference              # the real reference catalogue the frontend already shows
python -m scripts.seed --demo                   # SAMPLE editions, stories, briefing, campus formats
python -m scripts.seed --reference --demo --dry-run
```

- **Reference** data: the 14 tools, 6 foundational papers, 5 India ecosystem entries and 6 general career
  signals from `src/data/`. These are real, public organisations and papers with official links. No prices,
  ratings or benchmarks. `last_verified_at` stays null. In production this needs `--confirm-production`.
- **Demo** data: the frontend's sample content, every row marked `is_demo = true`, attributed to a
  fictional "Sample source" on the reserved `example.invalid` domain, with no original URLs and no audio.
  It is **refused outright when `APP_ENV=production`**, and hidden from the API unless
  `EXPOSE_DEMO_CONTENT=true` (which production also refuses). Demo rows show as `provenance: "sample"`,
  so the frontend keeps its SAMPLE labels.
- Seeding is repeatable: rows are inserted with `ON CONFLICT DO NOTHING` in a single transaction.
  Nothing is updated or deleted, so editorial changes survive a re-run.
- Fixtures are exported from the frontend so both sides stay identical:
  `node --experimental-strip-types scripts/export_frontend_fixtures.mjs`.

## Running the API

```bash
uvicorn app.main:app --reload --port 8000           # development
```

- Swagger UI: http://localhost:8000/docs · ReDoc: http://localhost:8000/redoc · spec: `/openapi.json`
- The app starts without a database; `/api/v1/health` then reports `not_configured` with HTTP 503.

Production command (one process per CPU core is a reasonable start):

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2 \
  --proxy-headers --forwarded-allow-ips="<your platform's proxy IP or *>"
```

`--proxy-headers` with a correct `--forwarded-allow-ips` makes `request.client` the real visitor, which
the rate limiter relies on. Only use `*` when the platform guarantees all traffic arrives through its proxy.

## API reference

Base path `/api/v1`. JSON is camelCase. Only `GET` exists; other methods return 405.

| Endpoint | Returns | Notes |
| --- | --- | --- |
| `GET /health` | `{status, version, database}` | Runs `SELECT 1`. 200 when the DB answers, 503 otherwise. Use as the readiness check. |
| `GET /health/live` | `{status, version}` | Never touches the DB. Use as the liveness check. |
| `GET /stories` | `Page<Story>` | `topic`, `edition` (slug), `featured`, `q`, `from`, `to` (dates), `sort` = `newest`·`oldest`·`relevance` |
| `GET /stories/{id or slug}` | `Story` | 404 if not public |
| `GET /search?q=` | `Page<Story>` + `query`, `mode: "keyword"` | `topic`, `edition`. Ranked full-text keyword search |
| `GET /editions` | `Page<Edition>` | Newest week first |
| `GET /editions/current` | `Edition` + `stories[]` | Latest public week; featured story first, then newest (`edition_position` breaks ties). 404 if none |
| `GET /editions/{slug}` | `Edition` + `stories[]` | 404 if not public |
| `GET /briefings/latest` | `Briefing` | `audioUrl` and `durationSeconds` stay null until real audio exists. 404 if none |
| `GET /tools` | `Page<Tool>` | `category`, `q` (name, maker, summary) |
| `GET /tools/{slug}` | `Tool` | `pricingNotes` only set together with `lastVerifiedAt` |
| `GET /research` | `Page<ResearchPaper>` | `topic`, `q`, `sort` = `newest`·`oldest` |
| `GET /research/{id or slug}` | `ResearchPaper` | `demonstrated` = what the paper reports; `plainLanguage` = our interpretation, `summaryOrigin` says who wrote it |
| `GET /india` | `Page<IndiaEntry>` | |
| `GET /career-signals` | `Page<CareerSignal>` | `kind`, `topic`. Learning suggestions, not job listings |
| `GET /campus` | `Page<CampusItem>` | Only approved items; demo formats need `EXPOSE_DEMO_CONTENT` |

**Pagination**: `page` (1-based, max 10 000) and `pageSize` (1–100, default 20). Every collection returns
`{items, page, pageSize, total, totalPages}`. Ordering always ends with the primary key, so pages are stable.

**Response shapes** match `src/lib/types.ts`. Identifiers the frontend uses are slugs (`id` of an
edition, tool, paper or entry; `editionId` of a story). Stories also carry their UUID `id`. Extra fields
(`tags`, `originalPublishedAt`, `weekEnd`, `publishedAt` on editions, `pricingNotes`, `doi`, ...) are additive.

**Errors** always look like:

```json
{"error": {"code": "validation_error", "message": "One or more request parameters are invalid.",
           "details": [{"loc": ["query", "pageSize"], "msg": "Input should be less than or equal to 100", "type": "less_than_equal"}]}}
```

Codes: `validation_error` (422), `not_found` (404), `method_not_allowed` (405), `rate_limited` (429, with
`Retry-After`), `database_unavailable` (503), `internal_error` (500). Rejected input, SQL, stack traces and
connection details are never returned.

### Publication rules (`app/repositories/visibility.py`)

- An **edition** is public when `status = 'published'` and `published_at <= now()`.
- A **story** is public when it is published, its `published_at <= now()`, **and its edition is public**.
  Stories without an edition are never public. Scheduling = setting a future `published_at`.
- A **briefing** is public when published and its edition is public.
- **Tools, papers, India entries, career signals, campus items** are public when `status = 'published'`.
- **Demo rows** (`is_demo`) are hidden unless `EXPOSE_DEMO_CONTENT=true`, which production rejects.
- Database CHECK constraints back this up: a published story must have `published_at`, a source and its
  student-impact text; a published edition must have `published_at`.

### Search

`/search` and `/stories?q=` use PostgreSQL full-text search: a generated, GIN-indexed `tsvector` over
title (weight A), summary (B) and body (C), queried with `websearch_to_tsquery('english', ...)`, so quoted
phrases, `OR` and `-exclusions` work and odd input never errors. It is **keyword** search: it matches word
stems, not meaning. Responses say `"mode": "keyword"`. Semantic search over embeddings is Phase 3; the
response envelope can carry a different `mode` then without breaking clients. Tool and research `q`
filters are simple case-insensitive substring matches with LIKE wildcards escaped.

## Database schema

| Table | Key columns and rules |
| --- | --- |
| `sources` | `slug` unique, `kind` (rss, official_blog, newsletter, research_repository, news_site, government, demo), lower-case `canonical_domain`, unique (`canonical_domain`, `kind`), unique `feed_url`, `is_active` |
| `editions` | `slug` unique, `number`, `week_start`/`week_end` (≤ 7 days), `status`, `published_at`, `is_demo`; unique (`number`, `is_demo`) and (`week_start`, `is_demo`) |
| `stories` | `slug` unique, `title`, `summary`, `content` (paragraphs split by blank lines), `topic`, `tags[]`, `status` (draft, in_review, published, rejected, withdrawn), `is_featured` (max one per edition), `edition_id` + `edition_position` (unique per edition), `source_id`, `original_url` + `canonical_url` (unique: duplicate-article guard), `original_published_at`, `published_at`, `reading_time_minutes`, optional image (all three fields or none), four `impact_*` fields, generated `search_vector` |
| `audio_briefings` | one per edition (cascade delete), `audio_url`/`duration_seconds` null until real audio, `transcript`, `status`, `is_demo` |
| `tools` | `slug`, `provider`, `category`, `good_for[]`, `limitations`, `access_notes`, `official_url`, `pricing_notes` (requires `last_verified_at`), `source_id`, `status`, `display_order` |
| `research_papers` | `slug`, `title`, `authors`, `organisation`, `published_on`, unique `url` and `doi`, `key_findings`, `limitations`, `plain_language_summary` + `summary_origin` (editorial / ai_assisted), `topic`, `status` |
| `india_entries`, `career_signals`, `campus_items` | Small editorial collections with `slug`, `status`, `display_order`; `career_signals.related_topics[]` is checked against the topic list; `campus_items.is_demo` |
| `ingestion_jobs` | One row per source per ingestion run: `source_key`, `job_type`, `status` (queued, running, succeeded, failed, cancelled, skipped), `attempt_count ≥ 0`, `started_at` ≤ `completed_at`, `error_summary` ≤ 2000 chars, `details` (counts + incremental cursor). At most one `running` row per source. No public endpoint. Never stores credentials or raw newsletter bodies |
| `ingestion_items` | **Temporary** hand-off queue for the next stage: `source_key`, `source_type` (rss, gmail), `title`, `original_url`, `canonical_url`, unique `dedup_key` (sha256 of the canonical URL), `publisher`, `published_at`, `excerpt` ≤ 2000, `feed_identifier`, `collected_at`, `processing_status`, `expires_at`, `extra`. Deleted automatically after `expires_at` |

All primary keys are UUIDs (`gen_random_uuid()`), all timestamps are `timestamptz` in UTC, and every table has
`created_at`/`updated_at` (maintained by a trigger). Vocabularies are text columns with CHECK constraints
(easier to evolve than native enums). Foreign keys are deliberate: stories `RESTRICT` deletion of their
edition and source, briefings cascade with their edition, tools/papers/jobs keep their rows (`SET NULL`)
when a source is deleted.

**Duplicate articles**: `app/services/urls.py` canonicalises URLs (lower-case host, no `www.`, https,
no fragment, default port or trailing slash, tracking parameters such as `utm_*` and `fbclid` removed,
remaining query sorted). Ingestion must store the result in `stories.canonical_url`, which is unique.

## Security model

- **No public write API.** Public endpoints are `GET` only. There are no publish, create, update or delete
  routes. The only non-GET route is the admin ingestion trigger (Phase 3.1), which does not exist unless
  `INGESTION_ADMIN_TOKEN` is set, requires that bearer token, accepts only configured source keys, and
  only stages items for later review. Content is written by the seed CLI, migrations, or server-side
  jobs, all using server-only credentials. Editorial review UI and Supabase Auth with verified role claims
  belong to the phase that adds publishing.
- Secrets come only from the environment; `DATABASE_URL` is a `SecretStr`, rejected URLs are not echoed,
  and logs show `host:port/db` only.
- CORS: explicit origins, `GET` only, no credentials. Wildcards fail at startup.
- Input: Pydantic-validated, bounded page sizes and query lengths; all SQL goes through SQLAlchemy with bound parameters.
- Invalid requests are rejected before a database connection is opened.
- Rate limiting is enforced in memory per worker (fixed one-minute window per client IP, at most 10 000
  tracked clients). With N workers or instances the effective limit is N×; add edge rate limiting at
  your host or CDN for a global limit. Health endpoints are exempt.
- Responses carry `X-Request-ID`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- Supabase Data API access to the tables is blocked by RLS (see Supabase setup).

## Tests

```bash
createdb aicampus_test                      # any local PostgreSQL 14+
export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/aicampus_test
pytest                                       # API, constraints, migrations, seed, contract, config, ingestion
pytest tests/test_ingestion_*.py             # ingestion only
pytest tests/test_processing_*.py            # processing and deduplication only
ruff check . && ruff format --check .
```

- The suite refuses to run database tests unless `TEST_DATABASE_URL` is on `localhost` and its name contains
  `test`. It rebuilds that database's schema from the Alembic migrations, runs each test in a rolled-back
  transaction, and creates/drops a scratch `<name>_migrations` database to test upgrade → downgrade → upgrade
  and that models and migrations have not drifted.
- Without `TEST_DATABASE_URL`, database tests are skipped and the rest (startup, routing, validation,
  CORS, rate limiting, error handling, config rules, URL canonicalisation) still run.
- `tests/test_contract.py` parses `src/lib/types.ts` and checks every endpoint's JSON has all required
  fields of the matching TypeScript interface, and that seeded API content equals the frontend sample files.
- Nothing in the suite talks to Supabase, Google or any feed: ingestion tests use `httpx.MockTransport`, a
  fake DNS resolver, a fake Gmail API/token endpoint and synthetic fixtures in `tests/fixtures/ingestion/`.
  Processing tests use synthetic articles (`tests/processing_helpers.py`, `tests/fixtures/processing/`).

## News ingestion (Phase 3.1)

Collects candidate AI news from configured RSS/Atom feeds and from the TLDR AI newsletter in Gmail,
normalises it, removes duplicates and stages it in `ingestion_items` for the next stage (summarisation,
review). **It never publishes anything** and never changes stories, editions or sources.

### Architecture

```
config/ingestion_sources.toml ──► sources.py (validate) ──► runner.py ── for each source, isolated ──┐
                                                                                                     │
   providers/rss.py   ── http.py (SSRF-safe, retries) ── feedparser ──────────────┐                   │
   providers/gmail.py ── http.py ── Gmail REST API ── parsers/tldr.py ────────────┤                   │
                                                                                  ▼                   │
                                   normalize.py (clean, validate URL, canonicalise, dedup key)        │
                                                                                  ▼                   │
                                   store.py ── ingestion_items (pending, expires) + ingestion_jobs ◄──┘
```

| Module | Responsibility |
| --- | --- |
| `app/ingestion/sources.py` | Typed source config (Pydantic), strict validation, unique keys and URLs |
| `app/ingestion/url_safety.py` | URL and IP validation (SSRF guard) |
| `app/ingestion/http.py` | The only network client: timeouts, size cap, retries with backoff, redirect re-validation |
| `app/ingestion/providers/rss.py` | Conditional GET, feedparser on bytes, entry extraction, incremental cursor |
| `app/ingestion/providers/gmail.py` | OAuth refresh, message listing with pagination, sender allow-list, cursor |
| `app/ingestion/parsers/tldr.py` | TLDR email HTML/text → article title, destination URL, blurb |
| `app/ingestion/normalize.py` | `RawItem` → `NormalizedItem`: text cleaning, URL checks, canonical URL, dates |
| `app/ingestion/store.py` | Postgres store (and an in-memory one for dry runs/tests): staging, dedup, jobs, retention |
| `app/ingestion/runner.py` | Orchestration, per-source outcome (succeeded/skipped/failed), structured logs |
| `app/api/v1/endpoints/admin_ingestion.py` | Token-protected trigger and status endpoints |
| `scripts/ingest.py`, `scripts/gmail_oauth.py` | CLIs |

Each normalised item carries: `source_key`, `source_type`, `title`, `original_url` (as published),
`canonical_url`, `publisher`, `published_at`, `excerpt`, `feed_identifier` (feed URL or `gmail:<message id>`),
`collected_at`, `processing_status` (`pending`) and small `extra` metadata (author, categories, read time,
newsletter date). Values that a source does not provide stay empty: a newsletter item has no `publisher` or
`published_at`, because TLDR does not state them (the email's date is kept as `extra.newsletter_date`).

### Storage policy

AI//CAMPUS does not archive raw news. Ingestion keeps only what the next stage needs, briefly:

| What | Why | How long | How it is deleted |
| --- | --- | --- | --- |
| `ingestion_items`: title, both URLs, publisher, date, excerpt (≤ 600 chars), small metadata. Never full articles or email bodies | A durable hand-off so a crash between stages loses nothing, and so duplicates are recognised across runs and sources | `INGESTION_ITEM_RETENTION_DAYS` (default 14) via `expires_at` | Every run starts by deleting rows whose `expires_at` has passed; `python -m scripts.ingest purge` does the same |
| `ingestion_jobs`: per-source outcome, counts, cursor (ETag, newest date, processed Gmail ids). No content | Source-level failure tracking and incremental fetching | `INGESTION_JOB_RETENTION_DAYS` (default 90); each source's latest success is kept as its cursor | Same purge step |

Email bodies are parsed in memory and discarded. Gmail tokens live only in environment variables and in
memory. Published content (`stories`, `editions`, `sources`) is read only, to skip articles whose canonical
URL is already in the bulletin.

### Database changes (migration 0002)

Additive only; no existing row changes. Apply with `alembic upgrade head` (the live Supabase database is
at `0001`).

- New table `ingestion_items` (above), RLS enabled with no policies like every other table, `updated_at` trigger.
- `ingestion_jobs.source_key` (which configured source), `ingestion_jobs.details` (JSONB counts + cursor),
  `skipped` added to the status values, and a partial unique index allowing one `running` job per source.

Why not fewer: the staging table is the hand-off to Stage 3.2 and the cross-run dedup record; the job columns
reuse the existing table for failure tracking and cursors instead of adding a state table. The unique index
is what makes concurrent runs (two schedulers, or the CLI and the admin endpoint) safe, and it works through
Supabase's transaction pooler, unlike session advisory locks.

### Ingestion settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `INGESTION_SOURCES_FILE` | `config/ingestion_sources.toml` | Source configuration (relative to `backend/`) |
| `INGESTION_ADMIN_TOKEN` | unset | Enables the admin endpoints. ≥ 32 characters. Unset = endpoints return 404 |
| `INGESTION_ITEM_RETENTION_DAYS` | `14` | Lifetime of staged items (1–90) |
| `INGESTION_JOB_RETENTION_DAYS` | `90` | Lifetime of job rows (7–730) |
| `INGESTION_JOB_STALE_MINUTES` | `60` | A `running` job older than this is marked failed (crash recovery) |
| `INGESTION_SCHEDULE_MINUTES` | `360` | Interval for `scripts.ingest schedule` (15–10 080) |
| `INGESTION_HTTP_TIMEOUT_SECONDS` | `15` | Connect (≤ 10 s) and read timeout; whole download ≤ 2× this |
| `INGESTION_MAX_RESPONSE_BYTES` | `5000000` | Response size cap (decompressed) |
| `INGESTION_MAX_RETRIES` | `3` | Retries on timeouts, connection errors, 429 and 5xx (backoff 1 s, 2 s, 4 s … ≤ 30 s, jittered, honours `Retry-After`) |
| `INGESTION_MAX_ITEMS_PER_SOURCE` | `100` | Per-run cap per source |
| `INGESTION_USER_AGENT` | `AI-CAMPUS-Ingestion/0.3` | Sent with every fetch |
| `INGESTION_HTTP_TRUST_ENV` | `false` | Use `HTTP(S)_PROXY` from the environment |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | unset | Gmail OAuth. All three, or Gmail sources are skipped |

Placeholders for all of them are in `.env.example`. None of them may be given a `NEXT_PUBLIC_` prefix or put in
the frontend.

### Configuring RSS and official sources

Sources live in `config/ingestion_sources.toml`, so adding one is a reviewed pull request, not an API call:

```toml
[[sources]]
key = "example-lab-blog"          # unique, lower-case-with-dashes; stored on every item and job
type = "rss"                      # RSS 0.9x/1.0/2.0 or Atom
name = "Example Lab Blog"
category = "official"             # official | research | government | news | newsletter
publisher = "Example Lab"         # optional; otherwise the feed's own title. Never guess
url = "https://lab.example.com/feed.xml"   # https only (allow_http = true to opt in per source)
enabled = true
max_items = 50                    # optional per-run cap
initial_lookback_days = 7          # first run only: ignore older archive entries
```

`python -m scripts.ingest check` validates the file (unknown fields, duplicate keys or URLs, non-HTTPS URLs
and internal hosts are errors). Before enabling a new feed, run `python -m scripts.ingest run --dry-run --source <key>`.

The shipped file contains arXiv cs.AI, OpenAI News and the Hugging Face blog (enabled), the Google Research
blog (disabled until checked) and the TLDR AI newsletter, plus a commented template for Indian government
and research sources. **These URLs could not be fetched from the environment this was built in**; run the
dry run once to confirm each before relying on it, and add Indian sources only after verifying their feeds.

### Configuring Gmail (TLDR AI) with OAuth 2.0

1. In [Google Cloud Console](https://console.cloud.google.com/), create (or pick) a project and enable the
   **Gmail API** (APIs & Services → Library).
2. **OAuth consent screen**: User type *External* (or *Internal* for a Workspace account), app name
   "AI//CAMPUS ingestion", add the scope `https://www.googleapis.com/auth/gmail.readonly` only, and add the
   mailbox that receives TLDR AI as a test user. In *Testing* mode Google expires refresh tokens after 7 days;
   publish the app (it stays unverified, which is fine for your own mailbox) for a long-lived token.
3. **Credentials** → Create credentials → OAuth client ID → Application type **Desktop app**. Note the client
   id and secret.
4. On your own computer (not the server), from `backend/`:
   ```bash
   export GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
   export GMAIL_CLIENT_SECRET=your-client-secret
   python -m scripts.gmail_oauth          # opens a browser; sign in with the newsletter mailbox
   ```
   It uses PKCE and a `http://127.0.0.1:8765/` loopback redirect, requests only `gmail.readonly`, and prints the
   refresh token once.
5. Put `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and `GMAIL_REFRESH_TOKEN` in `backend/.env` or your host's
   secret settings. Never in the repository, chat, or the frontend.
6. Check the query: in Gmail, search `from:dan@tldrnewsletter.com subject:"TLDR AI"` and confirm it finds the
   issues and that the sender address matches. If not, edit `query` and `allowed_senders` for `tldr-ai`.
7. Test: `python -m scripts.ingest run --dry-run --source tldr-ai`.

Revoking: remove the app at https://myaccount.google.com/permissions. The next run reports the source as
failed with "Google rejected the Gmail refresh token", and other sources keep working.

How it reads mail: the configured query plus `after:<epoch>` from the cursor, paginated (up to
`max_messages` per run, oldest first, backlog carried over), `From` checked against `allowed_senders`, HTML
part parsed (plain text as fallback), TLDR click-tracking links decoded locally without being followed,
sponsored entries skipped, processed message ids remembered in the cursor.

### Running ingestion manually

```bash
cd backend
python -m scripts.ingest check                          # validate configuration
python -m scripts.ingest run --dry-run                  # fetch + normalise + print; no database needed
python -m scripts.ingest run --dry-run --source arxiv-cs-ai --show 10
python -m scripts.ingest run                            # stage into ingestion_items (needs DATABASE_URL, migration 0002)
python -m scripts.ingest run --source openai-news --json
python -m scripts.ingest status                         # last outcome, consecutive failures, pending items per source
python -m scripts.ingest purge                          # retention clean-up on demand
```

Exit status is 0 when every attempted source succeeded, 1 when any failed, 2 for configuration problems.

Over HTTP (when `INGESTION_ADMIN_TOKEN` is set):

```bash
curl -X POST https://<api-host>/api/v1/admin/ingestion/runs \
  -H "Authorization: Bearer $INGESTION_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"sources": ["arxiv-cs-ai"], "wait": true}'          # wait=false (default) returns 202 and runs in the background
curl https://<api-host>/api/v1/admin/ingestion/status -H "Authorization: Bearer $INGESTION_ADMIN_TOKEN"
```

Only configured source keys are accepted (no URLs), one run per process at a time (409 otherwise), and the
routes are left out of the public OpenAPI schema. CORS stays `GET`-only, so browsers cannot call them.

### Scheduled ingestion

The API process does not schedule anything itself (with several workers that would multiply runs). Pick one:

- **Long-running worker**: `python -m scripts.ingest schedule` runs every `INGESTION_SCHEDULE_MINUTES` (±10%
  jitter), re-reads the source file each cycle, survives individual failures, and stops cleanly on SIGTERM.
  Deploy it as a background worker next to the API (same image, command `python -m scripts.ingest schedule`).
- **Platform cron** (Render/Railway cron job, Cloud Scheduler + Cloud Run job, a VM's crontab):
  `0 */6 * * * cd /app && python -m scripts.ingest run`.
- **External trigger**: any scheduler that can send the authenticated `POST /admin/ingestion/runs`.

Whichever you choose, overlapping runs are safe: a source already being collected elsewhere is reported as
skipped, and a crashed run's job is marked failed after `INGESTION_JOB_STALE_MINUTES`.

### Reliability and outcomes

- Per-source isolation: each source gets its own job row and transaction; any exception, including bugs, is
  caught and recorded as that source's failure, and the next source runs.
- Outcomes: **succeeded** (fetched; may have staged 0 new items, e.g. HTTP 304), **skipped** (disabled,
  Gmail not configured, or already running elsewhere), **failed** (with a safe error summary). The run is
  `succeeded`, `partial` or `failed`.
- Idempotency: unique `dedup_key` (canonical URL hash), conditional GET, newest-date cursor, processed Gmail
  ids, and a check against `stories.canonical_url`. Re-running immediately stages nothing new.
- Counts per source in the job's `details.counts`: `entries`, `candidates`, `invalid`, `inserted`,
  `duplicates`, `already_in_bulletin`, `skipped_old`, `over_limit`, `not_modified`, and Gmail message counts.
- Logs: one line per source with key, outcome and counts (JSON with `LOG_JSON=true`). Fetch logs show scheme
  and host only. Tokens, request headers, email content and full URLs with query strings are never logged.

### Security

- Secrets only in server-side environment variables (`SecretStr`), never logged or stored in the database.
- Gmail scope is `gmail.readonly`; the server never sends mail or changes labels.
- SSRF: only `https` (http per source by opt-in), ports 443/80, no credentials in URLs, no single-label or
  internal names (`localhost`, `.local`, `.internal`, metadata hosts), IP literals in any notation checked,
  every DNS answer must be public (private, loopback, link-local, CGNAT, multicast, reserved, IPv4-mapped,
  NAT64 and 6to4 forms rejected), redirects re-validated per hop (max 3), and the connected peer address is
  checked again before the body is read. Article links found in content are validated the same way
  (without DNS) and are never fetched in Stage 3.1.
- Limits: timeouts, 5 MB response cap, item caps, bounded text lengths, no cookies, no environment proxies.
- Untrusted content: feeds and emails are parsed as data only. HTML is reduced to plain text, scripts and
  styles are dropped, XML external entities are not expanded, and text that looks like instructions is
  stored as an ordinary excerpt for human review; nothing acts on it.
- Admin endpoints: off unless a ≥ 32-character token is set, constant-time comparison, configured keys only.

### Limitations and next steps

- The example feed URLs and the TLDR sender/query were not fetched live (the build sandbox blocks outbound
  feed traffic). Tests use synthetic fixtures modelled on the formats. Run the dry runs above once.
- The TLDR parser depends on TLDR's current layout (label suffixes such as "(4 minute read)" and
  `tracking.tldrnewsletter.com/CL0/` links). If it changes, the source reports zero items and a warning
  rather than guessing; update `parsers/tldr.py` with a real sample.
- DNS rebinding is narrowed (answers checked, then the connected peer checked) but the request line and
  headers are sent before the peer check. No secrets are sent to feed hosts.
- Staged items are not linked to `sources` rows; the stage that creates draft stories maps `source_key` and
  publisher to a source. Stage 3.2 moves items from `pending` to `processed` or `discarded`.

## News processing and deduplication (Phase 3.2)

Turns the items Stage 3.1 staged in `ingestion_items` into clean, deduplicated **story clusters** with every
source reference kept, for Stage 3.3 (the AI Intelligence Engine). It uses no AI API and makes no network
calls; every decision is deterministic and explained in the stored reasons. **It never publishes,
summarises, scores or deletes anything.**

### Architecture

```
ingestion_items (pending) ──► store.py (load unexpired, job lock) ──► pipeline.py, item by item:
     prepare   text.py      normalise + validate title, URLs (Stage 3.1 SSRF validator), publisher, dates, flags
     features  features.py  tokens, shingles, identifiers, entities, event type, content hash
     decide    matching.py  exact duplicate → near duplicate → same story → new cluster
──► ingestion_items (processed: cluster_id, dedup_relation, duplicate_of_id, content_hash, processing)
──► clusters.py builds StoryCluster records on read (contract.py) ──► Stage 3.3 / admin endpoint / CLI
```

| Module | Responsibility |
| --- | --- |
| `app/processing/text.py` | Readable text from HTML (drops nav, header, footer, cookie/share/related blocks; prefers `<article>`/`<main>`; keeps code, numbers and quotes), whitespace/entity/mojibake repair, title validation, publisher tidy-up, UTC dates, instruction-like content flags |
| `app/processing/features.py` | Tokens with light stemming, word 3-shingles, char trigrams, arXiv/DOI identifiers, organisation gazetteer, model/product names with versions, benchmarks, event-type lexicon, content hash |
| `app/processing/matching.py` | The three decision rules and their vetoes |
| `app/processing/pipeline.py` | Ordering, the exact-match index and blocking index, assignment, per-item failure isolation |
| `app/processing/clusters.py` | Clusters, representative selection, source references and sightings |
| `app/processing/contract.py` | The typed Stage 3.3 output schema (`StoryCluster`) |
| `app/processing/store.py`, `runner.py`, `service.py` | Postgres and in-memory stores, the run lock, entry points for Stage 3.3 |
| `app/api/v1/endpoints/admin_processing.py`, `scripts/process.py` | Token-protected endpoints and the CLI |

How Stage 3.1 output enters: every unexpired row of `ingestion_items` whose `processing_status` is `pending`
(or `failed` from an earlier attempt) is processed; rows already `processed` are context and keep their
decision. `scripts.ingest schedule --process` runs processing after each ingestion cycle, or run
`python -m scripts.process run` from cron after `scripts.ingest run`.

### Normalisation

Titles, excerpts and publishers are re-normalised defensively (Stage 3.1 already strips markup): HTML entities
decoded, NFKC, hyphen look-alikes unified, zero-width and bidi control characters removed, paragraphs kept,
UTF-8 mojibake ("â€™") repaired only when the round trip is clean. Missing or bad values are never filled in:

| Problem | Result |
| --- | --- |
| Empty, URL-only, generic ("Untitled", "Read more") or 1–2 character title | item `discarded` with a reason (`missing_title`, `title_is_url`, `generic_title`, `title_too_short`) |
| Unsafe or malformed URL (re-checked with Stage 3.1's `validate_url`: private/metadata IPs, `localhost`, credentials, odd ports, non-http schemes) | `discarded`, `unsafe_or_malformed_url` |
| Missing publisher | `publisher: null`; `site` (the URL's host) is reported separately and never presented as the publisher |
| Missing, unparseable, pre-1990 or future date | `publishedAt: null`; time windows then use the newsletter date, else the collection time, and `processing.timeBasis` says which |

Publication dates are UTC-aware. The URL as published (`originalUrl`) and the canonical URL are both kept.

### URL canonicalisation

`app/services/urls.py` (shared with Stage 3.1, so dedup keys stay consistent) lower-cases the host, drops
`www.` and the default port, removes fragments (hash-bang routes `#!/…` are kept, they select content),
removes trailing slashes, sorts the remaining query and removes tracking parameters: `utm_*`, `mc_*`, `pk_*`,
`hsa_*`, `oly_*`, `vero_*`, `_hs*`, `fbclid`, `gclid`, `gclsrc`, `dclid`, `gbraid`, `wbraid`, `msclkid`, `yclid`,
`twclid`, `ttclid`, `li_fat_id`, `igshid`, `mkt_tok`, `_ga`, `_gl`, `ref`, `ref_src`, `ref_url`, `cmpid`, `s_cid`,
`wickedid`, `mbid`. Other parameters (`?v=`, `?id=`, `?page=`) are kept. The canonical URL is a deduplication
key, never a link to show: it is always https. It is never invented; an item without a valid URL is discarded.

### Deduplication and clustering

Each item gets exactly one relation to its cluster:

| Relation | Meaning | Rule |
| --- | --- | --- |
| `same_url` (source reference only) | Another source carried the identical canonical URL | Stage 3.1 keeps one row per canonical URL; other sources are appended to `extra.also_seen` (max 10). Same feed or message again adds nothing |
| `exact_duplicate` | The same article under another URL | Same arXiv id (abs/pdf/any version) or DOI from the URL, **or** identical content hash (case-folded words of title + excerpt) when the excerpt has ≥ `PROCESSING_MIN_BODY_TOKENS` words |
| `near_duplicate` | A lightly modified copy | Excerpt word-3-shingle Jaccard ≥ 0.5 **and** title similarity ≥ 0.4, within 168 h. If an excerpt is missing: title similarity ≥ 0.85 **and** same site **and** within 48 h |
| `related_report` | A different article about the same event | Story rule below |
| `primary` | First article of a new story | Nothing matched |

**Story rule.** Within 96 h of the cluster's first report, with no veto against any member, the item must
match the founding report or at least half of the cluster's distinct reports, where a match is:
`0.40 × title-topic Jaccard + 0.35 × text-topic Jaccard + 0.25 × entity score ≥ 0.30` (+0.15 if no recognised
entity is shared, +0.10 if either event type is unknown). "Topic" words exclude organisation names and event
words, so "Company X launches A" and "Company X launches B" share nothing. The entity score is 1.0 for a
shared model/product name, otherwise half the entity Jaccard. Without a shared entity the titles must share
at least three topic words.

**How false merges are prevented.** Vetoes are checked first and always win:

- different known event types never merge (a launch, a pricing change and a safety report about the same
  model stay three clusters). Types: launch, update, pricing, safety, research, funding, acquisition,
  partnership, policy, legal, incident, people, from a title-weighted keyword lexicon;
- the same product family with different versions never merges ("GPT-5" vs "GPT-5.1", "Llama 3" vs "Llama 4");
  tiers (mini, pro) and parameter counts (405B) do not conflict;
- titles that both name products but share no product family never merge ("GPT-7" vs "Sora 3");
- near-duplicates also need equal title numbers ("raises $2B" vs "raises $5B");
- title similarity alone is never enough across sites, a content hash needs a long enough excerpt, and an
  organisation in common is weak evidence on its own;
- clusters are compared through their founding report or a majority of members, so chains cannot drift;
- no cluster is ever merged into another and no item is moved, so a decision Stage 3.3 has seen is stable.

When unsure, the pipeline keeps stories apart: a missed merge shows as two clusters, a wrong merge would hide
a story. All thresholds are settings (below). A semantic layer (embeddings) can be added later as one more
signal in `matching.story_match` without changing the pipeline, the stored columns or the contract.

### Similarity thresholds

| Variable | Default | Used for |
| --- | --- | --- |
| `PROCESSING_NEAR_DUPLICATE_TEXT_THRESHOLD` | `0.5` | Excerpt shingle Jaccard for a near-duplicate |
| `PROCESSING_NEAR_DUPLICATE_TITLE_THRESHOLD` | `0.4` | Title similarity required alongside it |
| `PROCESSING_TITLE_ONLY_THRESHOLD` | `0.85` | Title similarity for a same-site near-duplicate without excerpts |
| `PROCESSING_TITLE_ONLY_WINDOW_HOURS` | `48` | Window for that rule |
| `PROCESSING_NEAR_DUPLICATE_WINDOW_HOURS` | `168` | Window for text near-duplicates |
| `PROCESSING_STORY_THRESHOLD` | `0.3` | Story score |
| `PROCESSING_STORY_WINDOW_HOURS` | `96` | Story window, from the cluster's first report |
| `PROCESSING_MIN_BODY_TOKENS` | `15` | Excerpt words needed for content hashing and shingles |
| `PROCESSING_MAX_ITEMS` | `5000` | Staged items loaded per run (newest first) |

Raise a threshold to merge less. The values each run used are stored in its job row and in every cluster's
`processing.thresholds`. A synthetic run of 2 000 unrelated items sharing ten organisations and five verbs
produced no false merges and took about 7 s from cold.

### Output contract for Stage 3.3

`app.processing.service.list_story_clusters(settings, db, limit=100, updated_since=None)` returns
`list[StoryCluster]` (Pydantic, frozen, `extra="forbid"`; JSON is camelCase). Re-analyse a cluster only when its
`processing.revision` changes.

| Field | Type | Notes |
| --- | --- | --- |
| `contractVersion` | `"1.0"` | Bumped on breaking changes |
| `clusterId` | UUID | Deterministic (UUIDv5 of the first item's dedup key), stable across runs |
| `status` | `"ready"` | Deduplicated and clustered |
| `title`, `description` | string, string \| null | The representative's title and excerpt. **Untrusted** |
| `excerpt` | string \| null | Longest excerpt among non-duplicate sources. **Untrusted** |
| `representative` | object | `itemId`, `title`, `originalUrl`, `canonicalUrl`, `publisher`, `site`, `publishedAt`, `selectionReasons`. Chosen for metadata quality (official channel, publisher, date, excerpt). **Not** a claim of being the original |
| `originalSource` | null | Always null in Stage 3.2: who reported first is not verified |
| `eventType` | enum \| null | Majority event type of the members |
| `entities` | object | `organisations`, `products` (normalised, e.g. `gpt 5.1`), `benchmarks` |
| `earliestPublishedAt`, `latestPublishedAt` | datetime \| null | Earliest/latest *known* publication date among sources |
| `firstCollectedAt`, `lastCollectedAt` | datetime | |
| `articleCount`, `duplicateCount`, `sourceCount`, `publisherCount` | int | Distinct URLs; exact + near duplicates + same-URL sightings; references; distinct publishers (or sites) |
| `sources[]` | `SourceReference` | Every reference, see below |
| `contentFlags` | string[] | `possible_prompt_injection`, `chat_template_tokens` from any source |
| `contentIsUntrusted` | `true` | Always |
| `processing` | object | `algorithmVersion`, `processedAt`, `revision`, `timeBasis`, `thresholds` |

`SourceReference`: `itemId`, `ingestionDedupKey`, `sourceKey`, `sourceName`, `sourceType` (rss, gmail),
`sourceKind` (official_channel, research_repository, government, news_outlet, newsletter, unknown: what the
source is, from its configured category, not who broke the story), `feedIdentifier`, `title`, `excerpt`,
`publisher`, `site`, `originalUrl`, `canonicalUrl`, `publishedAt`, `collectedAt`, `newsletterDate`, `relation`,
`duplicateOf`, `matchScore`, `matchReasons`, `identifiers`, `contentFlags`, `metadata` (author, categories...).

Rules for Stage 3.3: put source text into prompts only as quoted, delimited data, never as instructions; link
readers to `originalUrl`; do not describe the representative or the earliest date as the original source.

### Storage policy and database changes (migration 0003)

No new table and no raw-news archive. Processing results are six columns on the temporary `ingestion_items`
rows and are deleted with them by Stage 3.1's retention (default 14 days, purged at the start of each
ingestion run). Clusters are assembled on read; a cluster ends when its last item expires. Published editions
and stories are untouched (they live in `stories`, with their own source fields, and Stage 3.5/3.6 handle
publishing and archive clean-up).

| Change | Why |
| --- | --- |
| `ingestion_items.cluster_id` (indexed), `dedup_relation` (CHECK), `duplicate_of_id` (self FK, SET NULL), `content_hash` (indexed), `processed_at`, `processing` (JSONB: entities, event type, identifiers, scores, reasons; **no article text**) | Stable, idempotent decisions across runs without a second table |
| CHECK: `processed` rows must have a cluster and relation | Stage 3.3 never sees a half-written decision |
| `ingestion_jobs.job_type` accepts `story_processing` | Each run is one job row (counts, thresholds); the existing one-running-job-per-key index is the cross-process lock |

Stage 3.1 change: `extra.also_seen` sightings (see the relation table). It needs no migration and Stage 3.1
works the same with or without migration 0003. **Migration 0003 is not applied to the live database yet**;
apply it with `alembic upgrade head` (or the Supabase connector) after review. Until then, processing reports
"apply migration 0003" and exits with status 2.

### Running processing

```bash
cd backend
python -m scripts.process dry-run                       # live Stage 3.1 dry run → in-memory processing; no DB, nothing stored
python -m scripts.process dry-run --source openai-news --json
python -m scripts.process run                           # process staged items (DATABASE_URL + migration 0003)
python -m scripts.process clusters --limit 20           # print current clusters; --json for the full contract
python -m scripts.ingest schedule --process             # scheduled ingestion followed by processing
```

Over HTTP, when `INGESTION_ADMIN_TOKEN` is set (404 otherwise; same bearer token; not in the OpenAPI schema):

```bash
curl -X POST https://<api-host>/api/v1/admin/processing/runs -H "Authorization: Bearer $INGESTION_ADMIN_TOKEN"
curl "https://<api-host>/api/v1/admin/processing/clusters?limit=20" -H "Authorization: Bearer $INGESTION_ADMIN_TOKEN"
```

### Security

- All source content is untrusted. Chat-template tokens (`<|im_start|>`, `[INST]`, `<<SYS>>`) are removed,
  instruction-like text is flagged (not acted on, not deleted) and the contract marks every text field untrusted.
- URLs are re-validated with Stage 3.1's SSRF validator; nothing in Stage 3.2 fetches a URL.
- Readable-text extraction is a parser, never a renderer: scripts, styles, iframes, forms and hidden elements
  are dropped and the output is plain text.
- No public endpoint exposes processing records; admin routes need the ≥ 32-character token.
- Logs contain counts only: no titles, excerpts, URLs or credentials. `processing` stores no article text.

### Limitations

- Stage 3.1 stores excerpts of at most 600 characters and does not fetch articles, so text signals use
  excerpts. `extract_readable_text` is ready for full pages if a later stage fetches them (through
  `SafeHttpClient`).
- The organisation gazetteer and the product/event lexicons are small and English-only. Unknown organisations
  only weaken the signal (a stricter score is then required); add names in `features.py`.
- An event described with words from two types (for example a launch announced together with new pricing)
  gets one type, so a pure-pricing report of the same day may stay a separate cluster. That is deliberate.
- Clusters are never merged after the fact, so two clusters created before a bridging report arrives stay two.
- The thresholds were tuned on synthetic data, not on live feeds. Run `scripts.process dry-run` on real feeds
  and adjust the settings if needed.

## Connecting the frontend

1. Run the API (`uvicorn app.main:app --port 8000`) with `CORS_ORIGINS` including the frontend origin.
2. In the repository root, create `.env.local` with `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`.
3. `npm run dev`. Every page now reads from the API through `src/lib/api/index.ts`.

Without `NEXT_PUBLIC_API_BASE_URL` the frontend keeps using its labelled sample and reference files.
With it set, failures show the existing error states; they never silently fall back to samples. Sections
whose API collection is empty (for example the campus section before any approved item exists) are hidden.

## Deployment

The backend is a standard ASGI app and does not depend on Vercel. Any Python or container host works.

1. Create the service from `backend/` (Dockerfile provided, or a Python buildpack with
   `pip install -r requirements.txt`).
2. Set environment variables: `APP_ENV=production`, `DATABASE_URL` (session pooler on IPv4-only hosts,
   transaction pooler + `DB_USE_NULL_POOL=true` on serverless), `CORS_ORIGINS=https://<your frontend>`,
   and `FORWARDED_ALLOW_IPS` for the Docker image. Leave `EXPOSE_DEMO_CONTENT` unset.
3. Release step (before traffic switches): `alembic upgrade head`.
4. Start command: see [Running the API](#running-the-api). The Dockerfile already uses it.
5. Health checks: readiness `GET /api/v1/health`, liveness `GET /api/v1/health/live`.
6. Set `NEXT_PUBLIC_API_BASE_URL` in the Vercel project to the backend URL and redeploy the frontend.
7. Optionally `python -m scripts.seed --reference --confirm-production` to load the reference catalogue.

Consider `DOCS_ENABLED=false` in production if you do not want the schema public (it contains nothing secret).

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `DATABASE_URL must be a PostgreSQL URL` at startup | Wrong scheme (for example `postgresql+asyncpg://`). Use `postgresql://`. |
| `could not translate host name "db.<ref>.supabase.co"` or network unreachable | The host has no IPv6. Use the session pooler URL. |
| `password authentication failed` | Wrong password, or the pooler user name is missing `.<project-ref>`. Copy the URI from Connect again; URL-encode special characters. |
| `prepared statement "..." already exists` | Transaction pooler with prepared statements on. Use port 6543 (auto-detected) or set `DB_PREPARED_STATEMENTS=false`. |
| `remaining connection slots are reserved` / pool timeouts | Too many workers × pool size. Lower `DB_POOL_SIZE`, or use the transaction pooler with `DB_USE_NULL_POOL=true`. |
| `/api/v1/health` returns 503 `unavailable` | Database unreachable, paused (free projects pause after inactivity) or credentials wrong. Check the logs: they name the exception type. |
| `relation "stories" does not exist` | Migrations not applied: `alembic upgrade head`. |
| Browser shows a CORS error | Add the exact frontend origin (scheme + host + port, no trailing slash) to `CORS_ORIGINS`. |
| API returns empty lists after seeding demo data | Expected: set `EXPOSE_DEMO_CONTENT=true` in a non-production environment. |
| `canceling statement due to statement timeout` | A query exceeded `DB_STATEMENT_TIMEOUT_MS`. Raise it or add an index. |

## Phase boundaries

Implemented: schema, migrations, public read API, keyword search, safe seed, tests, docs (Phase 2);
RSS/Atom and Gmail (TLDR AI) ingestion into a temporary staging table, scheduling, admin trigger (Phase 3.1);
normalisation, exact/near-duplicate detection, story clustering with provenance and the Stage 3.3 contract (Phase 3.2).
Not implemented (Phase 3.3+): LLM summaries, ranking, fact verification and editorial workflow, edition
generation, publishing (which will require authorised human approval), embeddings and semantic or
conversational search, audio generation. `ingestion_jobs`, the
`in_review`/`rejected` story states, `summary_origin`, `canonical_url` and the `mode` field of search
responses are the hooks those phases will use.
