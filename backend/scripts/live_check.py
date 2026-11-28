"""Live integration checks for the deployed AI//CAMPUS system (Stage 4.3). Read-only for real data.

    python -m scripts.live_check all                     # every check below that is configured
    python -m scripts.live_check db | gemini | embedding | gmail
    python -m scripts.live_check api --api-url https://API --site-url https://SITE
    python -m scripts.live_check site --site-url https://SITE --api-url https://API

Run it from GitHub Actions ("Live checks" workflow) so it uses the same hosted secrets as the scheduler. Safety:
- Database: every write goes into a TEMPORARY table inside a transaction that is always rolled back. No real row is
  inserted, changed or deleted. Real tables are only read (counts and the migration version).
- Gemini: exactly one tiny generation request and one embedding request (3 short texts) per run.
- Gmail: scope gmail.readonly. Lists at most 1 message id and reads only its From header. Nothing is sent, deleted,
  labelled or marked as read (the read-only scope cannot do that).
- HTTP checks send GET/OPTIONS only, plus admin requests without a valid token (they must be refused).
Never prints secrets, tokens, email addresses or message contents. Exit code 0 = all checks passed, 1 = a check
failed, 2 = nothing could be checked (configuration missing).
"""

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from scripts._cli import guarded

BACKEND = Path(__file__).resolve().parent.parent
SECRET_MARKERS = re.compile(r"postgres(ql)?://|sb_secret_|service_role|AIza[0-9A-Za-z_-]{20,}|refresh_token", re.I)


@dataclass
class Results:
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    published_editions: int | None = None  # from the database check, compared with the public API

    def error(self, name: str, exc: BaseException) -> None:
        """A check crashed: report only the exception type (messages can contain hosts or SQL)."""
        self.add(name, False, type(exc).__name__)
        if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
            traceback.print_exc()

    def add(self, name: str, ok: bool | None, detail: str) -> None:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.rows.append((name, status, detail))
        print(f"[{status}] {name}: {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for _, s, _ in self.rows)


def _settings():
    from app.core.config import get_settings

    return get_settings()


# --------------------------------------------------------------------------------------------------------- database


def _head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head() or "?"


def check_db(r: Results) -> None:
    from sqlalchemy import text

    from app.core.config import describe_database_target
    from app.db.session import Database

    settings = _settings()
    if settings.database_url is None:
        r.add("db.connect", None, "DATABASE_URL not set")
        return
    db = Database(settings.model_copy(update={"db_use_null_pool": True}))
    try:
        started = time.perf_counter()
        with db.engine.connect() as s:
            version = s.execute(text("show server_version")).scalar_one()
            ext = s.execute(text("select extversion from pg_extension where extname = 'vector'")).scalar_one_or_none()
            current = s.execute(text("select version_num from alembic_version")).scalar_one_or_none()
            published, unpublished = s.execute(
                text(
                    "select count(*) filter (where status = 'published' and not is_demo), "
                    "count(*) filter (where status <> 'published') from editions"
                )
            ).one()
            rls_off = s.execute(
                text(
                    "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity"
                )
            ).scalar_one()
            anon_grants = s.execute(
                text(
                    "select count(distinct table_name) from information_schema.role_table_grants "
                    "where table_schema = 'public' and grantee in ('anon', 'authenticated')"
                )
            ).scalar_one()
        ms = (time.perf_counter() - started) * 1000
        r.add("db.connect", True, f"{describe_database_target(settings)}, PostgreSQL {version}, {ms:.0f} ms")
        r.add("db.pgvector", ext is not None, f"vector extension {ext or 'MISSING'}")
        head = _head_revision()
        r.add("db.migrations", current == head, f"database at {current}, code head {head}")
        r.published_editions = published
        r.add("db.published_editions", published >= 1, f"{published} published, {unpublished} unpublished edition(s)")
        r.add("db.rls", rls_off == 0, f"{rls_off} public table(s) without row level security")
        r.add("db.data_api_grants", anon_grants == 0, f"{anon_grants} table(s) granted to anon/authenticated")

        # Write path: a TEMPORARY table in a transaction that is rolled back. Nothing persists.
        with db.engine.connect() as conn:
            tx = conn.begin()
            try:
                conn.execute(
                    text("create temporary table live_check_probe (id int primary key, note text) on commit drop")
                )
                conn.execute(text("insert into live_check_probe values (1, 'stage-4.3 live check')"))
                got = conn.execute(text("select note from live_check_probe where id = 1")).scalar_one()
                conn.execute(text("delete from live_check_probe where id = 1"))
                left = conn.execute(text("select count(*) from live_check_probe")).scalar_one()
            finally:
                tx.rollback()
        r.add(
            "db.write_read_delete",
            got == "stage-4.3 live check" and left == 0,
            "temporary row inserted, read, deleted, rolled back",
        )
    except Exception as exc:  # noqa: BLE001 - report, never crash the other checks
        r.error("db.connect", exc)
    finally:
        db.dispose()


# ----------------------------------------------------------------------------------------------------------- gemini


def check_gemini(r: Results) -> None:
    from app.intelligence.providers import build_provider
    from app.intelligence.providers.base import GenerationRequest, Message, ProviderError

    settings = _settings()
    if settings.gemini_api_key is None:
        r.add("gemini.generate", None, "GEMINI_API_KEY not set")
        return
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}, "word": {"type": "string"}},
        "required": ["answer", "word"],
    }
    request = GenerationRequest(
        model=settings.ai_model,
        messages=[
            Message("system", "Reply with JSON only."),
            Message("user", "What is 2 + 3? Put the number in 'answer' and the English word for it in 'word'."),
        ],
        schema_name="live_check",
        json_schema=schema,
        max_output_tokens=64,
        thinking_level=settings.ai_thinking_level or "minimal",
    )

    async def run():
        provider = build_provider(settings)
        try:
            name = await provider.check_model(settings.ai_model)
            started = time.perf_counter()
            response = await provider.generate(request)
            return name, response, (time.perf_counter() - started) * 1000
        finally:
            await provider.aclose()

    try:
        name, response, ms = asyncio.run(run())
    except ProviderError as exc:
        r.add("gemini.generate", False, f"{exc.kind}: {exc.safe_message}")
        return
    r.add("gemini.model", True, f"{name} available for this key")
    try:
        data = json.loads(response.content or "")
        valid = data.get("answer") == 5 and str(data.get("word", "")).strip().lower() == "five"
    except (ValueError, AttributeError):
        valid = False
    r.add(
        "gemini.generate",
        valid,
        f"structured JSON {'valid' if valid else 'INVALID'}, finish={response.finish_reason}, "
        f"{response.usage.input_tokens} in / {response.usage.output_tokens} out tokens, {ms:.0f} ms",
    )


def check_embedding(r: Results) -> None:
    from sqlalchemy import text

    from app.db.session import Database
    from app.intelligence.providers import build_provider
    from app.intelligence.providers.base import EmbeddingRequest, ProviderError

    settings = _settings()
    if settings.gemini_api_key is None:
        r.add("embedding.generate", None, "GEMINI_API_KEY not set")
        return
    texts = [
        "A new open-source large language model was released for code generation.",
        "Researchers published an open LLM that writes software code.",
        "The recipe needs two cups of flour and a pinch of salt.",
    ]

    async def run():
        provider = build_provider(settings)
        try:
            return await provider.embed(
                EmbeddingRequest(model=settings.embedding_model, texts=texts, dimensions=settings.embedding_dimensions)
            )
        finally:
            await provider.aclose()

    try:
        response = asyncio.run(run())
    except ProviderError as exc:
        r.add("embedding.generate", False, f"{exc.kind}: {exc.safe_message}")
        return
    dims = {len(v) for v in response.vectors}
    ok = len(response.vectors) == 3 and dims == {settings.embedding_dimensions}
    r.add("embedding.generate", ok, f"{len(response.vectors)} vector(s) of {sorted(dims)} dims, model {response.model}")
    if not ok or settings.database_url is None:
        return

    def unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return "[" + ",".join(f"{x / n:.7f}" for x in v) + "]"

    db = Database(settings.model_copy(update={"db_use_null_pool": True}))
    try:
        with db.engine.connect() as conn:
            tx = conn.begin()
            try:
                schema = conn.execute(
                    text(
                        "select n.nspname from pg_extension e join pg_namespace n on n.oid = e.extnamespace "
                        "where e.extname = 'vector'"
                    )
                ).scalar_one()
                # The real tables use pgvector from Supabase's `extensions` schema; resolve it for this transaction.
                conn.execute(text("select set_config('search_path', :p, true)"), {"p": f"pg_catalog, public, {schema}"})
                conn.execute(
                    text(
                        f"create temporary table live_check_vectors (id int primary key, "
                        f"embedding vector({int(settings.embedding_dimensions)})) on commit drop"
                    )
                )
                for i, v in enumerate(response.vectors[1:], start=2):
                    conn.execute(
                        text("insert into live_check_vectors values (:id, cast(:v as vector))"), {"id": i, "v": unit(v)}
                    )
                nearest = conn.execute(
                    text("select id from live_check_vectors order by embedding <=> cast(:q as vector) limit 1"),
                    {"q": unit(response.vectors[0])},
                ).scalar_one()
            finally:
                tx.rollback()
        r.add(
            "embedding.store_and_search", nearest == 2, f"nearest neighbour is text {nearest} (expected 2), rolled back"
        )
    except Exception as exc:  # noqa: BLE001
        r.error("embedding.store_and_search", exc)
    finally:
        db.dispose()


# ------------------------------------------------------------------------------------------------------------ gmail


def check_gmail(r: Results) -> None:
    from app.ingestion.errors import GmailAuthError
    from app.ingestion.http import SafeHttpClient
    from app.ingestion.providers.gmail import GmailClient, sender_allowed
    from app.ingestion.sources import GmailSourceConfig, load_sources

    settings = _settings()
    if not settings.gmail_configured:
        r.add("gmail.auth", None, "GMAIL_* not set")
        return
    source = next((s for s in load_sources(settings.ingestion_sources_file) if isinstance(s, GmailSourceConfig)), None)
    if source is None:
        r.add("gmail.auth", None, "no Gmail source configured")
        return
    http = SafeHttpClient.from_settings(settings)
    try:
        client = GmailClient(
            http,
            client_id=settings.gmail_client_id or "",
            client_secret=settings.gmail_client_secret.get_secret_value(),
            refresh_token=settings.gmail_refresh_token.get_secret_value(),
        )
        page = client._get("/messages", {"q": source.query, "maxResults": 1})  # noqa: SLF001 - one id only
        r.add("gmail.auth", True, "refresh token accepted, gmail.readonly access works")
        ids = [m["id"] for m in page.get("messages") or [] if isinstance(m.get("id"), str)]
        r.add("gmail.list", bool(ids), f"{len(ids)} message id(s) matched the newsletter query (max 1 requested)")
        if ids:
            message = client._get(f"/messages/{ids[0]}", {"format": "metadata", "metadataHeaders": "From"})  # noqa: SLF001
            known = sender_allowed(message, source.allowed_senders)
            r.add("gmail.read", known, f"newest match is from an allowed newsletter sender: {known} (headers only)")
    except GmailAuthError as exc:
        r.add("gmail.auth", False, f"sign-in refused: {exc}"[:200])
    except Exception as exc:  # noqa: BLE001
        r.error("gmail.auth", exc)
    finally:
        http.close()


# ------------------------------------------------------------------------------------------------------- HTTP checks


def _request(url: str, *, method: str = "GET", headers: dict | None = None, timeout: float = 30.0):
    if not url.startswith(("https://", "http://")):
        raise ValueError("Only http(s) URLs can be checked.")
    ua = {"User-Agent": "AI-CAMPUS-live-check", **(headers or {})}
    req = urllib.request.Request(url, method=method, headers=ua)  # noqa: S310 - scheme checked above
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https URLs given by the operator
            body = resp.read(2_000_000)
            return resp.status, dict(resp.headers), body, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read(200_000), (time.perf_counter() - started) * 1000


def _header(headers: dict, name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


def check_api(r: Results, api: str | None, site: str | None) -> None:
    if not api:
        r.add("api.health", None, "--api-url not given")
        return
    api = api.rstrip("/")
    r.add("api.https", api.startswith("https://"), api.split("://")[0])
    status, _, body, ms = _request(f"{api}/api/v1/health/live")
    r.add("api.liveness", status == 200, f"HTTP {status} in {ms:.0f} ms (includes any cold start)")
    status, _, body, ms = _request(f"{api}/api/v1/health")
    try:
        health = json.loads(body)
    except ValueError:
        health = {}
    r.add(
        "api.health_db",
        status == 200 and health.get("database") == "ok",
        f"HTTP {status}, database={health.get('database')}, version={health.get('version')}, {ms:.0f} ms",
    )

    status, _, body, _ = _request(f"{api}/api/v1/editions?pageSize=50")
    try:
        items = json.loads(body).get("items", [])
    except (ValueError, AttributeError):
        items = None
    count = len(items or [])
    r.add(
        "api.public_editions", status == 200 and items is not None and count >= 1, f"HTTP {status}, {count} edition(s)"
    )
    if r.published_editions is not None:
        r.add(
            "api.no_drafts_public",
            items is not None and count == r.published_editions,
            f"API lists {count}; database has {r.published_editions} published (drafts would make these differ)",
        )
    status, _, body, _ = _request(f"{api}/api/v1/editions/current")
    r.add("api.current_edition", status == 200, f"HTTP {status}")
    status, _, _, _ = _request(f"{api}/api/v1/stories?pageSize=5")
    r.add("api.public_stories", status == 200, f"HTTP {status}")
    r.add(
        "api.no_secrets_in_responses",
        not SECRET_MARKERS.search(body.decode("utf-8", "replace")),
        "no connection strings or keys in responses",
    )
    status, _, _, _ = _request(f"{api}/api/v1/does-not-exist")
    r.add("api.unknown_route", status == 404, f"HTTP {status}")

    for path in (
        "/api/v1/admin/editions",
        "/api/v1/admin/editions/session",
        "/api/v1/admin/cleanup/policy",
        "/api/v1/admin/ingestion/status",
    ):
        status, _, _, _ = _request(f"{api}{path}")
        r.add(f"api.admin_no_token {path}", status in (401, 403, 404), f"HTTP {status} (must be refused)")
    status, _, _, _ = _request(f"{api}/api/v1/admin/editions", headers={"Authorization": "Bearer not-a-real-token"})
    r.add("api.admin_bad_token", status in (401, 403, 404), f"HTTP {status} (must be refused)")
    status, _, _, _ = _request(f"{api}/api/v1/admin/editions/generate", method="POST")
    r.add("api.admin_write_no_token", status in (401, 403, 404, 405), f"HTTP {status} (must be refused)")

    if site:
        origin = site.rstrip("/")
        preflight = {
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        }
        status, headers, _, _ = _request(f"{api}/api/v1/admin/editions", method="OPTIONS", headers=preflight)
        allowed = _header(headers, "access-control-allow-origin")
        r.add(
            "api.cors_site_allowed",
            allowed == origin,
            f"HTTP {status}, allow-origin {'matches site' if allowed == origin else allowed}",
        )
        evil = {**preflight, "Origin": "https://evil.example"}
        status, headers, _, _ = _request(f"{api}/api/v1/admin/editions", method="OPTIONS", headers=evil)
        allowed = _header(headers, "access-control-allow-origin")
        r.add("api.cors_other_refused", allowed is None, f"HTTP {status}, allow-origin {allowed or 'absent'}")


def check_site(r: Results, site: str | None, api: str | None) -> None:
    if not site:
        r.add("site.home", None, "--site-url not given")
        return
    site = site.rstrip("/")
    status, headers, body, ms = _request(site + "/")
    html = body.decode("utf-8", "replace")
    r.add("site.https", site.startswith("https://"), site.split("://")[0])
    r.add("site.home", status == 200, f"HTTP {status} in {ms:.0f} ms")
    r.add("site.no_localhost", "localhost:8000" not in html and "127.0.0.1" not in html, "no localhost API in the page")
    r.add("site.no_secrets", not SECRET_MARKERS.search(html), "no keys or connection strings in the HTML")
    r.add(
        "site.security_headers",
        bool(_header(headers, "x-content-type-options")),
        f"x-content-type-options={_header(headers, 'x-content-type-options')}",
    )
    r.add("site.live_edition", "Sample edition." not in html, "home page is not showing the sample-edition notice")
    for path in ("/archive", "/admin"):
        status, _, _, _ = _request(site + path)
        r.add(f"site.page {path}", status == 200, f"HTTP {status}")
    story = next(iter(re.findall(r'href="(/stories/[a-z0-9-]+)"', html)), None)
    if story:
        status, _, page, _ = _request(site + story)
        links = re.findall(r'<a[^>]+href="(https://[^"]+)"', page.decode("utf-8", "replace"))
        sources = [u for u in links if site.split("://")[-1] not in u]
        r.add("site.story_page", status == 200, f"HTTP {status} for a story linked from the home page")
        r.add("site.source_links", bool(sources), f"{len(sources)} external source link(s) on that story page")
    else:
        r.add("site.story_page", False, "no story link found on the home page")
    if api:
        # The browser bundle must point at the hosted API: look for its host in the page's own JS chunks.
        host = api.split("://")[-1].rstrip("/")
        chunks = re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', html)[:40]
        found = host in html
        for chunk in chunks:
            if found:
                break
            _, _, js, _ = _request(site + chunk)
            found = host in js.decode("utf-8", "replace")
        r.add("site.uses_hosted_api", found, f"hosted API host {'found' if found else 'NOT found'} in the site's code")


def main() -> int:
    parser = argparse.ArgumentParser(prog="scripts.live_check", description=__doc__.split("\n")[0])
    parser.add_argument(
        "checks", nargs="*", default=["all"], choices=["all", "db", "gemini", "embedding", "gmail", "api", "site"]
    )
    parser.add_argument("--api-url")
    parser.add_argument("--site-url")
    args = parser.parse_args()
    wanted = set(args.checks)
    every = "all" in wanted
    r = Results()
    if every or "db" in wanted:
        check_db(r)
    if every or "gemini" in wanted:
        check_gemini(r)
    if every or "embedding" in wanted:
        check_embedding(r)
    if every or "gmail" in wanted:
        check_gmail(r)
    if every or "api" in wanted:
        check_api(r, args.api_url, args.site_url)
    if every or "site" in wanted:
        check_site(r, args.site_url, args.api_url)
    passed = sum(1 for _, s, _ in r.rows if s == "PASS")
    failed = sum(1 for _, s, _ in r.rows if s == "FAIL")
    print(f"\n{passed} passed, {failed} failed, {len(r.rows) - passed - failed} skipped")
    if not r.rows or passed + failed == 0:
        return 2
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(guarded(main, "Live check"))
