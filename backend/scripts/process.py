"""Phase 3.2 news processing: normalisation, deduplication and story clustering of staged items.

    python -m scripts.process run                         # process pending staged items (needs migration 0003)
    python -m scripts.process run --json                  # the run report as JSON
    python -m scripts.process clusters --limit 20         # current story clusters (the Stage 3.3 input)
    python -m scripts.process clusters --json             # the full Stage 3.3 contract as JSON
    python -m scripts.process dry-run                     # live Stage 3.1 dry run → processing in memory; no DB
    python -m scripts.process dry-run --source tldr-ai --show 10

Exit codes: 0 success, 1 failed run, 2 configuration problem. Nothing is ever published by this command.
"""

import argparse
import json
import sys

from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.ingestion.errors import SourceConfigError
from app.ingestion.runner import IngestionRunner
from app.ingestion.store import MemoryIngestionStore
from app.processing.contract import StoryCluster
from app.processing.models import StagedItem
from app.processing.runner import ProcessingRunner
from app.processing.store import MemoryProcessingStore, SchemaNotReady, SqlProcessingStore


def _database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        print("DATABASE_URL is not set. Use `dry-run` to test without a database.", file=sys.stderr)
        return None
    return Database(settings.model_copy(update={"db_use_null_pool": True}))


def _print_report(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
        return
    print(f"Processing {report.status}{' (dry run)' if report.dry_run else ''}")
    if report.reason:
        print(f"  {report.reason}")
    for key, value in report.counts.items():
        if value:
            print(f"  {key:<18} {value}")


def _print_clusters(clusters: list[StoryCluster], as_json: bool, show: int) -> None:
    if as_json:
        print(json.dumps([c.model_dump(mode="json", by_alias=True) for c in clusters], indent=2))
        return
    print(f"{len(clusters)} story cluster(s)")
    for c in clusters[:show]:
        event = c.event_type or "unclassified"
        print(f"\n* {c.title[:110]}")
        print(
            f"  {event} · {c.article_count} article(s), {c.source_count} source reference(s), "
            f"{c.duplicate_count} duplicate(s)"
        )
        if c.entities.organisations or c.entities.products:
            print(f"  entities: {', '.join(c.entities.organisations + c.entities.products)[:150]}")
        for ref in c.sources[:6]:
            who = ref.publisher or ref.site or "publisher unknown"
            date = ref.published_at.date().isoformat() if ref.published_at else "no date"
            print(f"    - [{ref.relation}] {who} · {ref.source_key} · {date}")
        if c.content_flags:
            print(f"  flags: {', '.join(c.content_flags)}")


def cmd_run(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        print(f"Processing staged items in {describe_database_target(settings)}", file=sys.stderr)
        report = ProcessingRunner(settings, SqlProcessingStore(db)).run()
        _print_report(report, args.json)
        if report.reason == SchemaNotReady.safe_message:
            return 2
        return 0 if report.status in ("succeeded", "skipped") else 1
    finally:
        db.dispose()


def cmd_clusters(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        clusters = ProcessingRunner(settings, SqlProcessingStore(db)).clusters(limit=args.limit)
        _print_clusters(clusters, args.json, args.limit)
        return 0
    except SchemaNotReady as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    finally:
        db.dispose()


def cmd_dry_run(settings: Settings, args) -> int:
    """Fetch with Stage 3.1 (dry run, nothing stored), then process and cluster in memory."""
    ingestion = IngestionRunner(settings, MemoryIngestionStore(), dry_run=True)
    ingest_report = ingestion.run(args.source)
    if not args.json:
        print(
            f"Stage 3.1 dry run {ingest_report.status}: "
            + ", ".join(f"{s.key}={s.counts.get('would_stage', 0)}" for s in ingest_report.sources)
        )
    staged: dict[str, StagedItem] = {}
    for items in ingestion.dry_run_items.values():
        for item in items:
            staged.setdefault(item.dedup_key, StagedItem.from_normalized(item))
    runner = ProcessingRunner(settings, MemoryProcessingStore(list(staged.values())), dry_run=True)
    report = runner.run()
    if not args.json:
        _print_report(report, False)
    _print_clusters(runner.clusters(limit=500), args.json, args.show)
    return 0 if report.status == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Print JSON.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Process pending staged items once.", parents=[common])
    clusters = sub.add_parser("clusters", help="Print the current story clusters.", parents=[common])
    clusters.add_argument("--limit", type=int, default=20)
    dry = sub.add_parser("dry-run", help="Stage 3.1 dry run + in-memory processing. No database.", parents=[common])
    dry.add_argument("--source", action="append", help="Source key (repeatable). Default: all.")
    dry.add_argument("--show", type=int, default=15, help="Clusters to print.")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {"run": cmd_run, "clusters": cmd_clusters, "dry-run": cmd_dry_run}
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
