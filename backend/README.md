# AI//CAMPUS backend (Phase 2)

A public, read-only REST API for the AI//CAMPUS bulletin, backed by Supabase PostgreSQL.
FastAPI · Pydantic v2 · SQLAlchemy 2 · psycopg 3 · Alembic · pytest.

The backend is independent of the Next.js frontend: it lives in `backend/`, has its own dependencies,
and deploys to any Python host. The frontend talks to it only through `NEXT_PUBLIC_API_BASE_URL`.

> **Status of the live database:** the schema, migrations and seed have been run and tested against a
> local PostgreSQL 16. They have **not** been run against a Supabase project yet, because no Supabase
> credentials were available. Follow [Supabase setup](#supabase-setup) to do that.

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
│   ├── api/v1/               Versioned router and endpoint modules
│   └── seed/                 Seed runner + JSON fixtures exported from the frontend's data files
├── migrations/               Alembic environment and versioned migrations
├── scripts/                  seed.py CLI, export_frontend_fixtures.mjs
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
`uv pip compile`); `requirements-dev.txt` adds the test tools. To upgrade, edit the `.in` file and recompile.

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
| `GET /editions/current` | `Edition` + `stories[]` | Latest public week; featured story first, then `edition_position`. 404 if none |
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
| `ingestion_jobs` | Phase 3 bookkeeping: `job_type`, `status`, `attempt_count ≥ 0`, `started_at` ≤ `completed_at`, `error_summary` ≤ 2000 chars. No public endpoint. Never stores credentials or raw newsletter bodies |

All primary keys are UUIDs (`gen_random_uuid()`), all timestamps are `timestamptz` in UTC, and every table has
`created_at`/`updated_at` (maintained by a trigger). Vocabularies are text columns with CHECK constraints
(easier to evolve than native enums). Foreign keys are deliberate: stories `RESTRICT` deletion of their
edition and source, briefings cascade with their edition, tools/papers/jobs keep their rows (`SET NULL`)
when a source is deleted.

**Duplicate articles**: `app/services/urls.py` canonicalises URLs (lower-case host, no `www.`, https,
no fragment, default port or trailing slash, tracking parameters such as `utm_*` and `fbclid` removed,
remaining query sorted). Ingestion must store the result in `stories.canonical_url`, which is unique.

## Security model

- **No write API.** Phase 2 exposes only public `GET` endpoints. There are no admin, publish, create,
  update or delete routes, so there is nothing for an anonymous client to abuse and no authentication
  to get wrong. Content is written by the seed CLI, migrations, or (Phase 3) server-side jobs, all using
  server-only credentials. Editorial review UI and Supabase Auth with verified role claims belong to the
  phase that adds write operations.
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
pytest                                       # API, constraints, migrations, seed, contract, config
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
- Nothing in the suite talks to Supabase.

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

Implemented: schema, migrations, public read API, keyword search, safe seed, tests, docs.
Not implemented (Phase 3+): Gmail/newsletter ingestion and OAuth, RSS discovery and scheduling, LLM
summaries, fact verification and editorial workflow, write/admin API and authentication, embeddings and
semantic or conversational search, audio generation, automated publishing. `ingestion_jobs`, the
`in_review`/`rejected` story states, `summary_origin`, `canonical_url` and the `mode` field of search
responses are the hooks those phases will use.
