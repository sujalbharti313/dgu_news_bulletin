"""Stage 3.6 cleanup and data retention: scheduled pass, dry run, status and job operations.

    python -m scripts.cleanup check               # validate config/retention.toml and the database schema
    python -m scripts.cleanup dry-run             # what cleanup would do now; writes NOTHING (safe on production)
    python -m scripts.cleanup scheduled           # what the daily scheduler runs (idempotent, publication-gated)
    python -m scripts.cleanup status              # recent cleanup jobs
    python -m scripts.cleanup show <job-id>       # one job: counts, manifest summary, audit trail
    python -m scripts.cleanup plan 2026-w47       # plan the cleanup of one published edition now (same delays)
    python -m scripts.cleanup cancel <job-id>     # cancel a job that has not run (nothing is deleted)
    python -m scripts.cleanup reopen <job-id>     # reopen a cancelled job (with a fresh manifest hold)
    python -m scripts.cleanup retry <job-id>      # retry a failed job

Unselected working data (Stage 3.3 analyses, Stage 3.4 verifications and embeddings, superseded ranking runs) of a
week is deleted only after its edition is confirmed published, the safety delay and minimum story age have passed,
the manifest has been held, and every record has been checked again at deletion time. Published editions, their
snapshots, stories, sources, links, selections and the audit logs are never deleted. No AI is used.
Exit codes: 0 ok (including "nothing to do"), 1 a cleanup job failed, 2 configuration or schema error.
"""

import argparse
import json
import sys

from sqlalchemy import select, text

from app.cleanup import build_cleanup_service
from app.cleanup.config import RetentionConfigError
from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.editions.config import EditionConfigError
from app.editions.service import Actor
from app.models import Edition

CLI = Actor("cli")


def _database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return None
    return Database(settings.model_copy(update={"db_use_null_pool": True}))


def _schema_ready(db: Database) -> bool:
    with db.write_session() as s:
        ok = s.scalar(text("SELECT to_regclass('cleanup_jobs') IS NOT NULL"))
    if not ok:
        print("The Stage 3.6 tables do not exist yet. Run `alembic upgrade head` first (migration 0008).")
    return bool(ok)


def _service(settings: Settings):
    db = _database(settings)
    if db is None or not _schema_ready(db):
        return None
    return build_cleanup_service(settings, db)


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    if isinstance(data, dict):
        for key, value in data.items():
            print(f"  {key}: {value}")


def cmd_check(args, settings: Settings) -> int:
    db = _database(settings)
    service = build_cleanup_service(settings, db) if db else None
    config = service.config if service else None
    if config is None:
        from app.cleanup.config import load_retention_config

        config = load_retention_config(settings.retention_config_file)
    pw = config.published_week
    print(f"Retention configuration OK (version {config.version}); cleanup {'ON' if config.enabled else 'OFF'}.")
    print(
        f"  Unselected working data of a published week: deleted after the later of published + "
        f"{pw.safety_delay_hours} h and week end + {pw.min_story_age_days} days, then a {pw.manifest_hold_hours} h "
        "manifest hold."
    )
    print(f"  Superseded ranking runs: {'deleted' if pw.delete_superseded_ranking_runs else 'kept'}.")
    print(f"  Orphans sweep: {'on' if config.orphans.enabled else 'off'} (min age {config.orphans.min_age_days} days).")
    print(f"  Stage 3.1 (unchanged): staged items {settings.ingestion_item_retention_days} days, job records "
          f"{settings.ingestion_job_retention_days} days.")  # fmt: skip
    print(f"  Database: {describe_database_target(settings)}")
    if db is None:
        return 0
    return 0 if _schema_ready(db) else 2


def cmd_dry_run(args, settings: Settings) -> int:
    service = _service(settings)
    if service is None:
        return 2
    data = service.dry_run()
    if args.json:
        _print(data, True)
        return 0
    print(f"Dry run (policy {data['policyVersion']}). Nothing was written.")
    for w in data["weeks"]:
        pub = w["publication"]
        print(f"  {w['edition']}: publication {'confirmed' if pub['ok'] else 'NOT confirmed ' + str(pub['reasons'])}")
        if "stories" in w:
            print(f"    earliest deletion {w['notBefore']}")
            print(f"    stories deletable if due now: {w['stories']['eligibleNow']}; kept: {w['stories']['kept']}")
            print(f"    ranking runs deletable: {w['rankingRuns']['eligibleNow']}; kept: {w['rankingRuns']['kept']}")
    o = data["orphans"]
    print(f"  Orphans: {o['stories']['eligibleNow']} stories, {o['verifications']['eligibleNow']} verification rows.")
    return 0


def cmd_scheduled(args, settings: Settings) -> int:
    service = _service(settings)
    if service is None:
        return 2
    report = service.scheduled_run(CLI if args.manual else None)
    _print(report.as_dict(), args.json)
    if not args.json:
        print(report.message)
    return 1 if report.status == "failed" else 0


def cmd_status(args, settings: Settings) -> int:
    service = _service(settings)
    if service is None:
        return 2
    jobs = service.list_jobs(args.limit)
    if args.json:
        _print(jobs, True)
        return 0
    if not jobs:
        print("No cleanup jobs yet.")
    for j in jobs:
        print(
            f"  {j['jobKey']:<22} {j['status']:<10} edition {j.get('editionSlug') or '-':<9} eligible {j['eligible']}"
            f" deleted {j['deleted']} protected {j['protected']} skipped {j['skipped']} failures {j['failures']}"
            f"  not before {j['notBefore']}"
            + (f"  ! {j['lastError']}" if j["lastError"] else "")
            + (f"  blocked: {j['blockedReasons']}" if j["blockedReasons"] else "")
        )
    return 0


def cmd_show(args, settings: Settings) -> int:
    service = _service(settings)
    if service is None:
        return 2
    _print(service.job_detail(args.job, limit=50), True)
    return 0


def cmd_plan(args, settings: Settings) -> int:
    service = _service(settings)
    if service is None:
        return 2
    with service.db.write_session() as s:
        edition_id = s.scalar(select(Edition.id).where(Edition.slug == args.edition, Edition.is_demo.is_(False)))
    if edition_id is None:
        print(f"No edition {args.edition}.", file=sys.stderr)
        return 2
    _print(service.plan_edition(edition_id, CLI), args.json)
    return 0


def cmd_job_action(args, settings: Settings) -> int:
    service = _service(settings)
    if service is None:
        return 2
    action = {"cancel": service.cancel, "reopen": service.reopen, "retry": service.retry}[args.command]
    _print(action(args.job, CLI), args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Print JSON.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate the configuration and schema.")
    sub.add_parser("dry-run", help="Show what would be deleted now; writes nothing.", parents=[common])
    sch = sub.add_parser("scheduled", help="Run one cleanup pass (the scheduler's job).", parents=[common])
    sch.add_argument("--manual", action="store_true", help="Record the pass as a CLI run instead of the scheduler.")
    st = sub.add_parser("status", help="Recent cleanup jobs.", parents=[common])
    st.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show", help="One cleanup job.")
    show.add_argument("job")
    plan = sub.add_parser("plan", help="Plan the cleanup of one published edition.", parents=[common])
    plan.add_argument("edition", help="Edition slug, e.g. 2026-w47.")
    for name in ("cancel", "reopen", "retry"):
        p = sub.add_parser(name, help=f"{name.capitalize()} a cleanup job.", parents=[common])
        p.add_argument("job")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {
        "check": cmd_check,
        "dry-run": cmd_dry_run,
        "scheduled": cmd_scheduled,
        "status": cmd_status,
        "show": cmd_show,
        "plan": cmd_plan,
        "cancel": cmd_job_action,
        "reopen": cmd_job_action,
        "retry": cmd_job_action,
    }
    try:
        return commands[args.command](args, settings)
    except (RetentionConfigError, EditionConfigError) as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CleanupError and friends: print the safe message only
        message = getattr(exc, "message", None)
        if message is None:
            raise
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    from scripts._cli import guarded

    sys.exit(guarded(main, "Cleanup"))
