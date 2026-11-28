# AI//CAMPUS production deployment (Stage 4.3)

Everything runs on free plans with no credit card and no billing account. Nothing here can create a charge: the
free plans used below stop or pause a service when a limit is reached; they never bill.

## 1. Architecture

```
 Students' browsers ──HTTPS──▶  Website (Next.js)            Vercel Hobby, project "dgu-news-bulletin"
                                   │ server-side fetch, cached 5 min
 Editor's browser (/admin) ──HTTPS─┼──────────────▶  API (FastAPI)          Vercel Hobby, 2nd project, Root Directory "backend"
          │ sign-in                 │                      │
          ▼                         ▼                      ▼
   Supabase Auth  ◀────── token check ──────  Supabase Postgres 17 + pgvector  (free project, ap-south-1)
                                                           ▲
 GitHub Actions (public repo, free) ───────────────────────┘
   daily 06:00 IST: edition → collect → process → AI analysis → embeddings + ranking → cleanup
   Monday 15:00 IST: edition retry          Monday 12:00 IST: live checks
        │                    │
        ▼                    ▼
   Gemini API (free AI Studio key)     Gmail API (read-only, TLDR AI mailbox)
```

Why this split: the API only answers short requests, which suits a serverless host that never "sleeps" in a way
that loses work. All long or scheduled work (minutes to an hour) runs in GitHub Actions, which is free without limit
for public repositories and already ran the real pipeline successfully (see section 9). No worker process, queue,
disk or extra service is needed, and none was added. The developer's PC is not involved in anything.

## 2. Services and why they were chosen

Free-tier facts below were read from each provider's own pages on 28 Nov 2026. Re-check them once a year.

| Service | Hosts | Relevant free limits | Sleeps? | Card needed? | Verdict |
|---|---|---|---|---|---|
| **Vercel Hobby** | website + API | 1M function invocations, 4 h active CPU, 360 GB-h memory, 100 GB transfer per month; functions up to 300 s; Python bundle 500 MB; 100 deploys/day | No sleep. Cold start of about 1-3 s after idle | No | **Selected**. Over a limit the feature pauses (30-day wait), never bills. Hobby is for "personal or non-commercial use" (see Limitations) |
| **GitHub Actions** | scheduler + all jobs | Standard runners free in public repos; job max 6 h | n/a | No | **Selected** (already in use). Cron can start late; schedules pause after 60 days without repo activity |
| **Supabase Free** | database + admin sign-in | 500 MB database, 5 GB egress, 50k auth users, 2 projects | Pauses after 1 week with no activity | No | **Kept** (existing project). Daily pipeline keeps it active. No automatic backups on Free |
| **Gemini API (AI Studio free tier)** | AI analysis, ranking, embeddings | Per key; this project keeps itself to 200 generation + 500 embedding requests/day (code guards) | n/a | No (do not enable billing) | **Kept** |
| Render Free | (candidate for API) | 750 instance-hours/month, 512 MB | Sleeps after 15 min, about 1 min to wake; may restart any time; no free cron | No | Rejected: slow first visit, adds nothing Vercel lacks |
| Fly.io | (candidate) | $5/month credit only with a card; without card a $1 credit that expires after 7 days | — | Yes for lasting use | Rejected (card / trial) |
| Railway | (candidate) | 30-day trial credit, then $1/month credit | — | — | Rejected (trial-based) |
| Google Cloud Run | (candidate) | Generous always-free tier | No | **Yes** (billing account) | Rejected (billing account required) |
| Cloudflare Workers | (candidate) | 100k requests/day, 10 ms CPU | No | No | Rejected: cannot run this Python/psycopg backend without a rewrite |
| Vercel Cron | (candidate scheduler) | Once per day per job, ±59 min | — | No | Not needed: GitHub Actions already runs the pipeline without the 300 s limit |

## 3. One-time setup (dashboard steps)

Do the steps in this order. Values written `LIKE_THIS` are entered by you in the dashboards; never paste them in
chat, screenshots, code or GitHub issues.

### 3.1 Supabase

1. **Authentication → Sign In / Providers → User Signups**: turn **off** "Allow new users to sign up". (Only the
   editor account is needed; admins are still checked against the `editorial_admins` list.)
2. **Connect** (top bar) → **Transaction pooler** → copy the URI (port **6543**) and put your database password into
   it. This is `DATABASE_URL` for the API. Keep the **Session pooler** URI (port 5432) for GitHub Actions and your PC.
3. **Project Settings → API Keys**: note the Project URL and the `sb_publishable_...` key (both public by design).
   Never use the `service_role` / `sb_secret_` key anywhere in this project.

### 3.2 API: second Vercel project

1. vercel.com → **Add New → Project** → import `sujalbharti313/dgu_news_bulletin` again (or open the existing second
   project `dgu-news-bulletin-bk83`).
2. **Settings → Build and Deployment**: **Root Directory = `backend`**, Framework Preset **Other** (or FastAPI if
   Vercel suggests it). Leave build and install commands empty.
3. **Settings → Environment Variables** (Environment: Production *and* Preview):

| Name | Value |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | Supabase **Transaction pooler** URI (port 6543) |
| `DB_USE_NULL_POOL` | `true` |
| `CORS_ORIGINS` | the website's address, e.g. `https://dgu-news-bulletin.vercel.app` (no trailing slash; comma-separate several) |
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_...` key |
| `GEMINI_API_KEY` | free AI Studio key (used only by the admin "Regenerate" button) |
| `DOCS_ENABLED` | `false` |
| `LOG_JSON` | `true` |

   Do **not** add `GMAIL_*`, `INGESTION_ADMIN_TOKEN` or `EXPOSE_DEMO_CONTENT` here: the API does not need them.
4. **Deployments → Redeploy**. Open `https://<api-domain>/api/v1/health`: it must show `"database":"ok"`.
5. **Settings → Deployment Protection**: keep "Standard Protection" (previews private, production public).

### 3.3 Website: existing Vercel project `dgu-news-bulletin`

1. **Settings → Environment Variables** (Production and Preview):

| Name | Value |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `https://<api-domain>` (no trailing slash) |
| `NEXT_PUBLIC_SITE_URL` | `https://<website-domain>` |
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase Project URL |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_...` key |

   Anything named `NEXT_PUBLIC_` is visible to every visitor, so only these four public values may use that prefix.
   Delete any other variable from this project (for example a database URL or Gemini key): the website does not use
   them.
2. **Deployments → Redeploy** (environment variables only apply to new builds).

### 3.4 GitHub

1. **Settings → Secrets and variables → Actions → Secrets** (already set): `DATABASE_URL` (Session pooler URI),
   `GEMINI_API_KEY`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`.
2. Same page → **Variables** tab → New repository variable: `SITE_URL` = website address, `API_URL` = API address.
3. **Actions → AI//CAMPUS live checks → Run workflow**. Every line must say PASS.
4. Optional: **Settings → Pages** → unpublish GitHub Pages (it is not part of AI//CAMPUS).

### 3.5 Gmail OAuth (production mode, stops the 7-day expiry)

The hosted system never shows a Google sign-in page: it uses a stored refresh token (GitHub secret), so no
redirect/callback URL has to be registered for production. The only loopback URL is used once, on a PC, by
`python -m scripts.gmail_oauth`. Scope: `gmail.readonly` only (cannot send, delete or change mail).

1. console.cloud.google.com → project **ai-campus-ingestion** → **Google Auth Platform → Branding**: fill App name
   (`AI//CAMPUS ingestion`), User support email and Developer contact email (both `ainewsdgu@gmail.com`). Save.
2. **Audience → Publish app → Confirm** (status becomes "In production"). Free; no verification is needed for one
   mailbox using it, but the sign-in screen shows "Google hasn't verified this app": click Advanced → Continue.
3. On the PC: `cd backend`, `.venv\Scripts\Activate`, then `$t = (python -m scripts.gmail_oauth)[-1]`, sign in as
   ainewsdgu@gmail.com, save `$t` into `backend\.env` as `GMAIL_REFRESH_TOKEN`, run `$t | Set-Clipboard` and paste it
   into the GitHub secret `GMAIL_REFRESH_TOKEN`.
4. Run **Actions → AI//CAMPUS live checks** (checks: gmail). The token then lasts until the mailbox password changes,
   access is revoked, or it is unused for 6 months.

### 3.6 Rotate secrets that appeared in screenshots

| Secret | Where to rotate | Then update |
|---|---|---|
| Supabase database password | Supabase → Project Settings → Database → Reset password | GitHub secret `DATABASE_URL`, API project `DATABASE_URL`, PC `backend\.env` |
| Gemini key | aistudio.google.com/apikey → create new key, delete old | GitHub secret `GEMINI_API_KEY`, API project `GEMINI_API_KEY`, PC `.env` |
| Gmail client secret | done 28 Nov 2026 | — |
| `INGESTION_ADMIN_TOKEN` | not used by the hosted system; leave it unset everywhere except your PC, or delete it | — |

## 4. Environment variables (complete list)

| Variable | API (Vercel) | GitHub Actions | Website (Vercel) | Secret? |
|---|---|---|---|---|
| `APP_ENV=production` | yes | yes (in workflow) | — | no |
| `DATABASE_URL` | yes, port 6543 | yes, port 5432 | **never** | **yes** |
| `DB_USE_NULL_POOL=true` | yes | yes (in workflow) | — | no |
| `CORS_ORIGINS` | yes | — | — | no |
| `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` | yes | — | as `NEXT_PUBLIC_…` | no (public) |
| `GEMINI_API_KEY` | yes | yes | **never** | **yes** |
| `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN` | no | yes | **never** | **yes** |
| `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_SITE_URL` | — | — | yes | no |
| `DOCS_ENABLED=false`, `LOG_JSON=true` | yes | — | — | no |
| `SITE_URL`, `API_URL` (repository variables) | — | yes | — | no |

Every other setting has a safe default (see `backend/.env.example`). `.env` files are git-ignored; only
`.env.example` files with placeholders are committed.

## 5. Schedules

| Job | When (Asia/Kolkata) | Runs on | Notes |
|---|---|---|---|
| Weekly edition | daily 06:00 check; generates on Monday for the previous Mon 00:00–Sun 23:59 week; retry Monday 15:00 | GitHub Actions | Idempotent: one edition per week; never approves or publishes |
| Collection (RSS + TLDR AI Gmail) | daily 06:00 | GitHub Actions | Incremental (ETag/cursor, processed message ids); duplicates rejected by unique keys |
| Processing, AI analysis, embeddings, ranking | daily, after collection | GitHub Actions | Free-quota guards; leftovers continue next day |
| Cleanup | daily, last step | GitHub Actions | Only weeks whose edition is published, after max(published+72 h, week end+35 d) + 24 h hold; published editions protected by database triggers |
| Live checks | Monday 12:00 + on demand | GitHub Actions | Read-only for real data |

Times in the workflow files are UTC cron lines; the week logic is in Python using Asia/Kolkata. GitHub often starts
scheduled runs late (observed 1-21 hours); everything is idempotent and catches up, and **Run workflow** starts a run
at once. Only one pipeline run can execute at a time (`concurrency`), and each stage also takes a database lock.

## 6. Health checks and logs

- API liveness: `https://<api-domain>/api/v1/health/live` (no database). Readiness: `/api/v1/health` (runs
  `SELECT 1`; 503 if the database is unreachable).
- API logs: Vercel → API project → **Logs** (Hobby keeps runtime logs for 1 hour) and **Deployments → Build Logs**.
- Pipeline logs: GitHub → **Actions** → a run → job `pipeline` (public; contain only counts and public headlines).
- Database: Supabase → **Logs**, **Reports** (size, connections), **Advisors**.
- Alerts: a red GitHub Actions run emails the repository owner. Runs turn red when a stage fails, including an
  expired Gmail sign-in (ingestion exit code 4) and any failed live check.

## 7. Deploying changes, redeploying and recovery

- **Normal change**: merge a pull request into `main`. Vercel rebuilds both projects automatically; GitHub Actions
  uses the new code on its next run. Database changes (`backend/migrations`) are never applied automatically: run
  `alembic upgrade head` from the PC *before* merging code that needs them.
- **Failed build**: Vercel keeps serving the last good deployment. Fix and push, or Vercel → Deployments → a good
  deployment → **Promote to Production** (instant rollback).
- **Supabase paused** (after a week without activity, e.g. if GitHub disabled the schedule): Supabase dashboard →
  project → **Restore**. Then GitHub → Actions → enable the workflow if it shows as disabled.
- **Gmail stopped**: see `docs/HANDOVER_GUIDE.md`.
- **Gemini quota used up**: nothing to do; work continues the next day (the daily limit resets at midnight Pacific
  time, 13:30 IST).
- **Backups**: the Free plan has no automatic backups. Once a month (and before risky work) export from the PC with
  the free PostgreSQL tools: `pg_dump "<session pooler URI>" -Fc -f aicampus.dump`. Published editions are also kept
  as immutable revision snapshots inside the database.

## 8. Security summary

HTTPS everywhere (Vercel and Supabase certificates). CORS lists exact origins, no wildcards. Admin pages need a
Supabase sign-in **and** a row in `editorial_admins`; the API verifies every token with Supabase and refuses
otherwise (401/403), with a per-admin write rate limit. Public endpoints return only published, non-demo content.
All Supabase tables have RLS on and no grants to the `anon`/`authenticated` roles, so the public publishable key
cannot read data directly. Secrets live only in Vercel/GitHub settings and the PC's `.env`; responses and logs never
contain them. Gmail uses the read-only scope. Fetching of article URLs is guarded against SSRF (public hosts only,
size and redirect limits). AI output is validated in code (no invented URLs or numbers) and an editor must approve
every edition.

## 9. Verification

`python -m scripts.live_check` (workflow "AI//CAMPUS live checks") tests the database (connection, pgvector,
migration head, RLS, a rolled-back write), Gemini (one structured request), embeddings (one batch stored in a
temporary table and searched with pgvector), Gmail (read-only sign-in and one header), the API (health, public data,
no drafts, admin refusal, CORS) and the website (pages, source links, uses the hosted API, no secrets).
