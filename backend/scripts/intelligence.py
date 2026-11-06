"""Phase 3.3 AI intelligence engine: analyse Stage 3.2 story clusters into validated story intelligence.

    python -m scripts.intelligence check                  # configuration and categories (no calls, no DB)
    python -m scripts.intelligence run                    # analyse clusters that need it (needs migration 0004)
    python -m scripts.intelligence run --retry-failed     # also retry failed / invalid stories now
    python -m scripts.intelligence list --limit 10        # stored intelligence (the Stage 3.4 input)
    python -m scripts.intelligence list --json
    python -m scripts.intelligence records                # per-story status, attempts, errors
    python -m scripts.intelligence dry-run --limit 2      # analyse current DB clusters, store NOTHING

`run` and `dry-run` call the AI provider and cost money (bounded by AI_MAX_STORIES_PER_RUN and, when prices
are set, AI_MAX_COST_PER_RUN_USD). Exit codes: 0 success, 1 failed or partial run, 2 configuration problem.
Nothing is ever published by this command.
"""

import argparse
import json
import sys
from datetime import UTC, datetime

from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.intelligence.categories import load_categories
from app.intelligence.engine import engine_key
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.runner import IntelligenceRunner, check_ready
from app.intelligence.service import build_runner
from app.intelligence.store import MemoryIntelligenceStore, SchemaNotReady, SqlIntelligenceStore
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore


def _database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return None
    return Database(settings.model_copy(update={"db_use_null_pool": True}))


def _print_report(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
        return
    print(f"Intelligence run {report.status}{' (dry run, nothing stored)' if report.dry_run else ''}")
    if report.reason:
        print(f"  {report.reason}")
    for key, value in report.counts.items():
        print(f"  {key:<26} {value}")
    if report.usage:
        u = report.usage
        cost = f", estimated ${u['estimated_cost_usd']:.4f}" if u.get("cost_estimated") else ""
        tokens = f"{u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out tokens"
        print(f"  usage: {u.get('calls', 0)} call(s), {tokens}{cost}")


def _print_intelligence(items, as_json: bool) -> None:
    if as_json:
        print(json.dumps([i.model_dump(mode="json", by_alias=True) for i in items], indent=2))
        return
    print(f"{len(items)} analysed stor{'y' if len(items) == 1 else 'ies'}")
    for i in items:
        print(f"\n* {i.headline}")
        print(f"  [{i.status}] {i.classification.primary_label} · evidence {i.evidence.sufficiency} · "
              f"{i.evidence.corroboration.replace('_', ' ')}")  # fmt: skip
        if i.summary:
            print(f"  {i.summary[:400]}")
        for idea in i.project_ideas:
            print(f"  idea (AI suggestion): {idea.title}")
        if i.review_flags:
            print(f"  flags: {', '.join(i.review_flags)}")
        for s in i.sources[:4]:
            print(f"    {s.ref_id} [{s.relation}] {s.publisher or s.site or 'publisher unknown'} · {s.original_url}")


def cmd_check(settings: Settings, args) -> int:
    try:
        categories = load_categories(settings.intelligence_categories_file)
    except IntelligenceConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    keys = ", ".join(categories.keys)
    print(f"Categories: {len(categories.categories)} ({keys}), max secondary {categories.max_secondary}")
    print(f"Engine key: {engine_key(categories, settings)}")
    print(f"Provider: {settings.ai_provider} at {settings.ai_base_url}")
    print(f"Model: {settings.ai_model or 'NOT SET'}   repair model: {settings.ai_repair_model or '(same)'}")
    print(f"API key: {'configured' if settings.openai_api_key else 'NOT SET'}")
    priced = (
        settings.ai_input_cost_per_million_tokens is not None and settings.ai_output_cost_per_million_tokens is not None
    )
    print(f"Limits: {settings.ai_max_stories_per_run} stories/run, concurrency {settings.ai_max_concurrency}, "
          f"{settings.ai_max_output_tokens} output tokens, timeout {settings.ai_request_timeout_seconds:.0f}s, "
          f"cost estimates {'on' if priced else 'off (set AI_*_COST_PER_MILLION_TOKENS)'}")  # fmt: skip
    print(f"Database: {describe_database_target(settings)}")
    try:
        check_ready(settings)
    except IntelligenceConfigError as exc:
        print(f"Not ready: {exc.safe_message}", file=sys.stderr)
        return 2
    print("Ready.")
    return 0


def cmd_run(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        print(f"Analysing story clusters in {describe_database_target(settings)}", file=sys.stderr)
        report = build_runner(settings, db).run(retry_failed=args.retry_failed, limit=args.limit)
        _print_report(report, args.json)
        if report.status == "failed" and report.job_id is None:
            return 2
        return 0 if report.status in ("succeeded", "skipped") else 1
    finally:
        db.dispose()


def cmd_dry_run(settings: Settings, args) -> int:
    """Real provider calls on current clusters, results printed and kept in memory only."""
    db = _database(settings)
    if db is None:
        return 2
    try:
        processing = ProcessingRunner(settings, SqlProcessingStore(db))
        runner = IntelligenceRunner(
            settings,
            MemoryIntelligenceStore(),
            lambda: processing.clusters(limit=settings.processing_max_items),
            dry_run=True,
        )
        report = runner.run(limit=args.limit)
        if not args.json:
            _print_report(report, False)
        _print_intelligence(runner.list(limit=args.limit), args.json)
        return 0 if report.status == "succeeded" else 1
    finally:
        db.dispose()


def cmd_list(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        runner = IntelligenceRunner(settings, SqlIntelligenceStore(db), list)
        _print_intelligence(runner.list(limit=args.limit), args.json)
        return 0
    except SchemaNotReady as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    finally:
        db.dispose()


def cmd_records(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        store = SqlIntelligenceStore(db)
        store.check_schema()
        rows = store.list(now=datetime.now(UTC), statuses=None, limit=args.limit)
        print(f"{len(rows)} record(s)")
        for r in rows:
            errors = ", ".join(e.get("code", "?") for e in (r.errors or []))
            line = (
                f"  {r.cluster_id} {r.status:<22} attempts={r.attempt_count} tokens={r.input_tokens}/{r.output_tokens}"
            )
            if errors:
                line += f" errors={errors}"
            if r.review_flags:
                line += f" flags={','.join(r.review_flags)}"
            print(line)
        return 0
    except SchemaNotReady as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    finally:
        db.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Print JSON.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Show configuration; no calls.")
    run = sub.add_parser("run", help="Analyse clusters that need it.", parents=[common])
    run.add_argument("--retry-failed", action="store_true", help="Retry failed/invalid stories now.")
    run.add_argument("--limit", type=int, default=None, help="At most this many stories (<= AI_MAX_STORIES_PER_RUN).")
    dry = sub.add_parser("dry-run", help="Analyse current clusters; store nothing.", parents=[common])
    dry.add_argument("--limit", type=int, default=2)
    lst = sub.add_parser("list", help="Print stored intelligence.", parents=[common])
    lst.add_argument("--limit", type=int, default=20)
    rec = sub.add_parser("records", help="Per-story status and errors.")
    rec.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {"check": cmd_check, "run": cmd_run, "dry-run": cmd_dry_run, "list": cmd_list, "records": cmd_records}
    try:
        return commands[args.command](settings, args)
    except IntelligenceConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
