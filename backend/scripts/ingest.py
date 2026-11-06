"""Phase 3.1 news ingestion (RSS/Atom feeds and the TLDR AI newsletter via Gmail).

    python -m scripts.ingest check                         # validate config/ingestion_sources.toml
    python -m scripts.ingest run --dry-run                 # fetch + normalise, print, store nothing (no DB)
    python -m scripts.ingest run                           # all enabled sources -> ingestion_items
    python -m scripts.ingest run --source arxiv-cs-ai      # one source (repeatable)
    python -m scripts.ingest schedule                      # run every INGESTION_SCHEDULE_MINUTES until stopped
    python -m scripts.ingest schedule --process            # ... and run Stage 3.2 processing after each cycle
    python -m scripts.ingest purge                         # delete expired staged items and old job rows
    python -m scripts.ingest status                        # last outcome per source

Exit codes: 0 every attempted source succeeded, 1 some or all failed, 2 configuration problem.
Nothing is ever published by this command.
"""

import argparse
import json
import logging
import random
import signal
import sys
import threading
from datetime import UTC, datetime

from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.ingestion.errors import SourceConfigError
from app.ingestion.runner import IngestionRunner
from app.ingestion.sources import load_sources, resolve_sources_path
from app.ingestion.store import MemoryIngestionStore, SqlIngestionStore

logger = logging.getLogger("app.ingestion.cli")


def _database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        print("DATABASE_URL is not set. Use `run --dry-run` to test without a database.", file=sys.stderr)
        return None
    # A CLI run holds a connection briefly per step; let the pooler own connections.
    return Database(settings.model_copy(update={"db_use_null_pool": True}))


def _print_report(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
        return
    print(
        f"Run {report.status}{' (dry run)' if report.dry_run else ''}: "
        f"purged {report.purged_items} expired item(s), {report.purged_jobs} old job row(s)"
    )
    for s in report.sources:
        counts = ", ".join(f"{k}={v}" for k, v in s.counts.items() if v)
        print(f"  {s.outcome.value:<9} {s.key:<28} {counts}")
        if s.reason:
            print(f"            {s.reason}")


def cmd_check(settings: Settings, _args) -> int:
    path = resolve_sources_path(settings.ingestion_sources_file)
    sources = load_sources(settings.ingestion_sources_file)
    print(f"{path.name}: {len(sources)} source(s) valid")
    for s in sources:
        configured = (
            ""
            if s.type != "gmail"
            else ("  gmail: configured" if settings.gmail_configured else "  gmail: NOT configured (will be skipped)")
        )
        print(f"  {'on ' if s.enabled else 'off'} {s.type:<6} {s.category:<10} {s.key}{configured}")
    return 0


def cmd_run(settings: Settings, args) -> int:
    if args.dry_run:
        runner = IngestionRunner(settings, MemoryIngestionStore(), dry_run=True)
        report = runner.run(args.source)
        _print_report(report, args.json)
        if not args.json:
            for key, items in runner.dry_run_items.items():
                print(f"\n[{key}] {len(items)} item(s) would be staged")
                for item in items[: args.show]:
                    date = item.published_at.date().isoformat() if item.published_at else "no date"
                    publisher = item.publisher or "publisher unknown"
                    print(f"  - {item.title[:100]}\n    {item.canonical_url}\n    {publisher} · {date}")
        return 0 if report.status == "succeeded" else 1
    db = _database(settings)
    if db is None:
        return 2
    try:
        print(f"Ingesting into {describe_database_target(settings)}", file=sys.stderr)
        report = IngestionRunner(settings, SqlIngestionStore(db)).run(args.source)
        _print_report(report, args.json)
        return 0 if report.status == "succeeded" else 1
    finally:
        db.dispose()


def cmd_schedule(settings: Settings, args) -> int:
    interval = (args.interval_minutes or settings.ingestion_schedule_minutes) * 60
    db = _database(settings)
    if db is None:
        return 2
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logger.info("scheduled ingestion every %d minute(s); stop with Ctrl+C or SIGTERM", interval // 60)
    try:
        while not stop.is_set():
            try:
                # Sources are re-read each cycle so config changes apply without a restart.
                report = IngestionRunner(settings, SqlIngestionStore(db)).run()
                _print_report(report, args.json)
                if args.process:
                    # Stage 3.2 over everything staged so far; failures are recorded and logged, never fatal.
                    from app.processing.runner import ProcessingRunner
                    from app.processing.store import SqlProcessingStore

                    ProcessingRunner(settings, SqlProcessingStore(db)).run()
            except SourceConfigError as exc:
                logger.error("source configuration invalid, skipping this cycle: %s", exc.safe_message)
            except Exception:  # noqa: BLE001 - keep the scheduler alive; the next cycle retries
                logger.exception("ingestion cycle failed")
            # +/-10% jitter so several deployments do not hit the same feeds in lock-step.
            stop.wait(interval * random.uniform(0.9, 1.1))  # noqa: S311 - jitter, not security
    finally:
        db.dispose()
    return 0


def cmd_purge(settings: Settings, _args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        store = SqlIngestionStore(db)
        now = datetime.now(UTC)
        items = store.purge_expired_items(now)
        jobs = store.purge_old_jobs(now, settings.ingestion_job_retention_days)
        print(f"Deleted {items} expired staged item(s) and {jobs} old job row(s).")
        return 0
    finally:
        db.dispose()


def cmd_status(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        sources = load_sources(settings.ingestion_sources_file)
        statuses = SqlIngestionStore(db).source_statuses([s.key for s in sources])
        for s in sources:
            st = statuses[s.key]
            print(
                f"  {s.key:<28} last={st.last_status or '-':<9} failures={st.consecutive_failures} "
                f"pending={st.pending_items} last_success={st.last_success_at or '-'}"
            )
            if st.last_error and st.last_status == "failed":
                print(f"      {st.last_error}")
        return 0
    finally:
        db.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Print reports as JSON.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate the source configuration.", parents=[common])
    run = sub.add_parser("run", help="Run ingestion once.", parents=[common])
    run.add_argument("--source", action="append", help="Source key (repeatable). Default: all.")
    run.add_argument("--dry-run", action="store_true", help="Fetch and normalise only; no database needed.")
    run.add_argument("--show", type=int, default=5, help="Items to print per source in a dry run.")
    schedule = sub.add_parser("schedule", help="Run ingestion repeatedly until stopped.", parents=[common])
    schedule.add_argument("--interval-minutes", type=int, help="Override INGESTION_SCHEDULE_MINUTES.")
    schedule.add_argument("--process", action="store_true", help="Run Stage 3.2 processing after each cycle.")
    sub.add_parser("purge", help="Delete expired staged items and old job rows.", parents=[common])
    sub.add_parser("status", help="Show the last outcome of each source.", parents=[common])
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {"check": cmd_check, "run": cmd_run, "schedule": cmd_schedule, "purge": cmd_purge, "status": cmd_status}
    try:
        return commands[args.command](settings, args)
    except SourceConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    except ValueError as exc:  # unknown --source key
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
