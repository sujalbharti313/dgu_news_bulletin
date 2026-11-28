"""Stage 3.5 weekly editions: scheduled generation, status, and editorial admin accounts.

    python -m scripts.edition check                      # validate config/edition.toml, show the schedule
    python -m scripts.edition scheduled                  # what the weekly scheduler runs (idempotent)
    python -m scripts.edition generate --week previous   # generate one week's edition now (if it has none)
    python -m scripts.edition generate --week 2026-11-16 --force   # regenerate (draft/review/rejected/failed only)
    python -m scripts.edition status                     # recent editions and their workflow status
    python -m scripts.edition show 2026-w47              # an edition's current revision
    python -m scripts.edition admins list
    python -m scripts.edition admins add you@example.com --name "Your Name"   # must have a Supabase Auth user
    python -m scripts.edition admins deactivate you@example.com

`scheduled` generates the edition of every due week (the last complete Monday-Sunday week in Asia/Kolkata, once
Monday 06:00 local has passed, plus `catch_up_weeks` earlier weeks) that has no edition yet, retries failed
generations after their backoff, and does nothing otherwise. When no completed Stage 3.4 ranking exists for the
week, it runs Stage 3.4 for that week first. Gemini (free tier only) is used for one composition request per
edition; without quota the edition waits, without a key it is built from the Stage 3.3 text.

Nothing here approves or publishes: only a signed-in editorial admin can do that, in the admin pages.
Exit codes: 0 ok (including "nothing to do" and "waiting"), 1 a generation failed, 2 configuration error.
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.editions import build_service
from app.editions.config import EditionConfigError, load_edition_config
from app.editions.schedule import due_at, due_weeks, last_complete_week, week_for
from app.editions.service import Actor
from app.models import Edition, EditorialAdmin


def _database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return None
    return Database(settings.model_copy(update={"db_use_null_pool": True}))


def _schema_ready(db: Database) -> bool:
    with db.write_session() as s:
        ok = s.scalar(text("SELECT to_regclass('edition_revisions') IS NOT NULL"))
    if not ok:
        print("The Stage 3.5 tables do not exist yet. Run `alembic upgrade head` first (migration 0007).")
    return bool(ok)


def cmd_check(args, settings: Settings) -> int:
    config = load_edition_config(settings.edition_config_file)
    sch = config.schedule
    now = datetime.now(UTC)
    last = last_complete_week(now, sch)
    print(f"Edition configuration OK (version {config.version}).")
    print(f"  Time zone {sch.timezone}; weeks run Monday 00:00 to Sunday 23:59 local time.")
    print(
        f"  Last complete week: {last.label}; due from {due_at(last, sch).astimezone(sch.tz):%A %d %b %Y %H:%M} local."
    )
    print(f"  Due now: {[w.start_date.isoformat() for w in due_weeks(now, sch)] or 'none'}")
    print(f"  Up to {config.selection.target_stories} stories from the Stage 3.4 shortlist.")
    print(
        f"  Gemini composition: {'on' if config.generation.use_ai else 'off'} (model {settings.ai_model}); "
        f"key {'set' if settings.gemini_api_key else 'NOT set (Stage 3.3 text will be used)'}."
    )
    print(f"  Admin sign-in (Supabase Auth): {'configured' if settings.admin_auth_configured else 'NOT configured'}.")
    print(f"  Database: {describe_database_target(settings)}")
    return 0


def _print_reports(reports, as_json: bool) -> None:
    if as_json:
        print(json.dumps([r.as_dict() for r in reports], indent=2))
        return
    if not reports:
        print("No edition is due right now.")
    for r in reports:
        print(f"[{r.status.upper()}] week of {r.week_start.isoformat()}: {r.message}")


def cmd_scheduled(args, settings: Settings) -> int:
    db = _database(settings)
    if db is None or not _schema_ready(db):
        return 2
    reports = build_service(settings, db).scheduled_run()
    _print_reports(reports, args.json)
    return 1 if any(r.status == "failed" for r in reports) else 0


def cmd_generate(args, settings: Settings) -> int:
    db = _database(settings)
    if db is None or not _schema_ready(db):
        return 2
    service = build_service(settings, db)
    try:
        window = week_for(args.week, datetime.now(UTC), service.config.schedule)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = service.generate_week(window.start_date, actor=Actor("cli"), force=args.force)
    _print_reports([report], args.json)
    return 1 if report.status == "failed" else 0


def cmd_status(args, settings: Settings) -> int:
    db = _database(settings)
    if db is None or not _schema_ready(db):
        return 2
    rows = build_service(settings, db).list_editions(args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No Stage 3.5 editions yet.")
    for r in rows:
        extra = f" · error: {r['lastError']}" if r["lastError"] else ""
        print(
            f"#{r['number']:>3} {r['slug']} {r['weekStart']}..{r['weekEnd']} {r['status']:<17} "
            f"rev {r['currentRevision']} · {r['storyCount']} stories{extra}"
        )
    return 0


def cmd_show(args, settings: Settings) -> int:
    db = _database(settings)
    if db is None or not _schema_ready(db):
        return 2
    with db.write_session() as s:
        edition = s.scalar(select(Edition).where(Edition.slug == args.edition, Edition.is_demo.is_(False)))
        if edition is None:
            try:
                edition = s.get(Edition, uuid.UUID(args.edition))
            except ValueError:
                edition = None
        if edition is None:
            print("Edition not found.", file=sys.stderr)
            return 1
        edition_id = edition.id
    data = build_service(settings, db).detail(edition_id)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    content = data["content"] or {}
    print(f"{data['slug']} · {data['status']} · revision {data['currentRevision']}")
    print(f"Title: {content.get('title')}")
    print(f"Introduction: {content.get('introduction')}")
    for i, st in enumerate(content.get("stories", []), start=1):
        print(f"  {i}. {st['text']['headline']}  [{st['publisher']}] {st['source_url']}")
    for issue in data["issues"]:
        print(f"  {issue['severity'].upper()}: {issue['path']} {issue['message']}")
    return 0


def cmd_admins(args, settings: Settings) -> int:
    db = _database(settings)
    if db is None or not _schema_ready(db):
        return 2
    with db.write_session() as s:
        if args.action == "list":
            for a in s.scalars(select(EditorialAdmin).order_by(EditorialAdmin.email)).all():
                print(f"{'active  ' if a.is_active else 'inactive'} {a.email} ({a.display_name or '-'}) {a.user_id}")
            return 0
        email = (args.email or "").strip().lower()
        if not email or "@" not in email:
            print("Give the admin's email address.", file=sys.stderr)
            return 2
        if args.action == "deactivate":
            admin = s.scalar(select(EditorialAdmin).where(EditorialAdmin.email == email))
            if admin is None:
                print("No such admin.", file=sys.stderr)
                return 1
            admin.is_active = False
            print(f"Deactivated {email}.")
            return 0
        user_id = args.user_id
        if user_id is None:
            has_auth = s.scalar(text("SELECT to_regclass('auth.users') IS NOT NULL"))
            if not has_auth:
                print("No auth.users table here; pass --user-id (the Supabase Auth user id).", file=sys.stderr)
                return 2
            user_id = s.scalar(text("SELECT id FROM auth.users WHERE lower(email) = :e"), {"e": email})
            if user_id is None:
                print(
                    "No Supabase Auth user with that email. Create it first: Supabase dashboard -> Authentication "
                    "-> Users -> Add user (email + strong password, auto-confirm).",
                    file=sys.stderr,
                )
                return 1
        admin = s.get(EditorialAdmin, uuid.UUID(str(user_id)))
        if admin is None:
            s.add(EditorialAdmin(user_id=uuid.UUID(str(user_id)), email=email, display_name=args.name, is_active=True))
        else:
            admin.email, admin.is_active = email, True
            if args.name:
                admin.display_name = args.name
        print(f"{email} is now an active editorial admin.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Print JSON.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate the configuration and show the schedule.")
    sub.add_parser("scheduled", help="Generate every due edition (the weekly scheduler's job).", parents=[common])
    gen = sub.add_parser("generate", help="Generate one week's edition.", parents=[common])
    gen.add_argument("--week", default="previous", help="previous (default) or a date YYYY-MM-DD in the week.")
    gen.add_argument("--force", action="store_true", help="Regenerate an existing draft/review/rejected edition.")
    st = sub.add_parser("status", help="Recent editions.", parents=[common])
    st.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show", help="One edition's current revision.", parents=[common])
    show.add_argument("edition", help="Slug (e.g. 2026-w47) or id.")
    adm = sub.add_parser("admins", help="Manage editorial admins.")
    adm.add_argument("action", choices=["list", "add", "deactivate"])
    adm.add_argument("email", nargs="?")
    adm.add_argument("--name", default=None)
    adm.add_argument("--user-id", default=None, help="Supabase Auth user id (only if auth.users is not readable).")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {
        "check": cmd_check,
        "scheduled": cmd_scheduled,
        "generate": cmd_generate,
        "status": cmd_status,
        "show": cmd_show,
        "admins": cmd_admins,
    }
    try:
        return commands[args.command](args, settings)
    except EditionConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    from scripts._cli import guarded

    sys.exit(guarded(main, "Edition scheduler"))
