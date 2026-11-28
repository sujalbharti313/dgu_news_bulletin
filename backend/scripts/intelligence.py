"""Phase 3.3 AI intelligence engine: analyse Stage 3.2 story clusters with Google Gemini.

    python -m scripts.intelligence check                  # configuration and categories (no calls, no DB)
    python -m scripts.intelligence check --ping           # also confirm AI_MODEL exists for your key (no quota)
    python -m scripts.intelligence dry-run --limit 2      # analyse current DB clusters, store NO analyses
    python -m scripts.intelligence run                    # analyse clusters that need it (needs migration 0005)
    python -m scripts.intelligence run --retry-failed     # also retry failed / invalid stories now
    python -m scripts.intelligence list --limit 10        # stored analyses (the Stage 3.4 input)
    python -m scripts.intelligence list --json
    python -m scripts.intelligence records                # per-story status, attempts, errors
    python -m scripts.intelligence usage                  # today's Gemini usage against the configured limits

`run` and `dry-run` send requests to Gemini. They stay inside the free tier by pacing requests
(AI_REQUESTS_PER_MINUTE), counting them per day (AI_REQUESTS_PER_DAY, AI_TOKENS_PER_DAY; dry runs count too)
and stopping when Google reports the daily quota as used up. Nothing is billed and nothing is published.
Exit codes: 0 success, 1 failed, partial or quota-limited run, 2 configuration problem.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.intelligence.categories import load_categories
from app.intelligence.engine import engine_key
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.priority import WEIGHTS
from app.intelligence.providers import build_provider
from app.intelligence.providers.base import ProviderError
from app.intelligence.quota import MemoryUsageLedger, SqlUsageLedger
from app.intelligence.runner import AnalysisRunner, check_ready
from app.intelligence.service import build_runner
from app.intelligence.store import MemoryAnalysisStore, SchemaNotReady, SqlAnalysisStore
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
    print(f"Analysis run {report.status}{' (dry run, no analyses stored)' if report.dry_run else ''}")
    if report.reason:
        print(f"  {report.reason}")
    for key, value in report.counts.items():
        print(f"  {key:<28} {value}")
    if report.usage:
        u = report.usage
        print(f"  usage: {u.get('calls', 0)} call(s), {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out")
        today = u.get("today") or {}
        if today:
            limit = today.get("requestsPerDayLimit") or "no limit"
            print(f"  today ({today.get('quotaDay')} Pacific): {today.get('requests')} request(s) of {limit}")
    for alert in report.alerts:
        print(f"  ALERT: {alert['message']}")


def _print_analyses(items, as_json: bool) -> None:
    if as_json:
        print(json.dumps([i.model_dump(mode="json", by_alias=True) for i in items], indent=2))
        return
    print(f"{len(items)} analysed stor{'y' if len(items) == 1 else 'ies'}")
    for i in items:
        p = i.priority_assessment
        print(f"\n* {i.headline}")
        print(
            f"  [{i.analysis_status}] {i.primary_category} · priority {p.overall_score} ({p.priority_level}, "
            f"preliminary) · evidence {i.evidence.sufficiency} · {i.evidence.corroboration.replace('_', ' ')}"
        )
        if i.summary:
            print(f"  {i.summary[:500]}")
        for idea in i.project_ideas:
            print(f"  project idea (AI-generated suggestion): {idea.title}")
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
    print(f"Categories: {len(categories.categories)} ({', '.join(c.label for c in categories.categories)})")
    print(f"Engine key: {engine_key(categories, settings)}")
    print(
        f"Provider: Google Gemini API (google-genai SDK)   model: {settings.ai_model}   "
        f"thinking level: {settings.ai_thinking_level or 'model default'}   fallback: none"
    )
    print(f"API key (GEMINI_API_KEY): {'configured' if settings.gemini_api_key else 'NOT SET'}")
    print(
        f"Free-tier guards: {settings.ai_requests_per_minute or 'no'} requests/minute, "
        f"{settings.ai_requests_per_day or 'no'} requests/day, {settings.ai_tokens_per_day or 'no'} tokens/day, "
        f"{settings.ai_max_stories_per_run} stories/run, concurrency {settings.ai_max_concurrency}"
    )
    print(
        f"Token limits: {settings.ai_max_input_tokens} input (estimated), {settings.ai_max_output_tokens} output; "
        f"timeout {settings.ai_request_timeout_seconds:.0f}s, {settings.ai_max_retries} retries"
    )
    print("Priority weights: " + ", ".join(f"{k} {v:.0%}" for k, v in WEIGHTS.items()))
    print(f"Database: {describe_database_target(settings)}")
    try:
        check_ready(settings)
    except IntelligenceConfigError as exc:
        print(f"Not ready: {exc.safe_message}", file=sys.stderr)
        return 2
    if args.ping:

        async def ping() -> str:
            provider = build_provider(settings)
            try:
                return await provider.check_model(settings.ai_model)
            finally:
                await provider.aclose()

        try:
            name = asyncio.run(ping())
        except ProviderError as exc:
            print(f"Model check failed: {exc.safe_message}", file=sys.stderr)
            return 2
        print(f"Model check: Gemini knows {name} for this key.")
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
    """Real Gemini calls on current clusters; analyses printed and kept in memory only. Usage is still counted in
    the daily ledger (when migration 0005 is applied), because the requests are real."""
    db = _database(settings)
    if db is None:
        return 2
    try:
        processing = ProcessingRunner(settings, SqlProcessingStore(db))
        ledger = SqlUsageLedger(db)
        if not ledger.check_schema():
            print("Migration 0005 not applied: daily usage is counted for this run only.", file=sys.stderr)
            ledger = MemoryUsageLedger()
        runner = AnalysisRunner(
            settings,
            MemoryAnalysisStore(),
            lambda: processing.clusters(limit=settings.processing_max_items),
            ledger=ledger,
            dry_run=True,
        )
        report = runner.run(limit=args.limit)
        _print_report(report, args.json)
        _print_analyses(
            runner.list(limit=args.limit, statuses=["completed", "needs_review", "insufficient_evidence"]), args.json
        )
        return 0 if report.status == "succeeded" else 1
    finally:
        db.dispose()


def cmd_list(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        runner = AnalysisRunner(settings, SqlAnalysisStore(db), list)
        _print_analyses(runner.list(limit=args.limit), args.json)
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
        store = SqlAnalysisStore(db)
        store.check_schema()
        rows = store.list(now=datetime.now(UTC), statuses=None, limit=args.limit)
        print(f"{len(rows)} record(s)")
        for r in rows:
            line = (
                f"  {r.cluster_id} {r.status:<22} attempts={r.attempt_count} tokens={r.input_tokens}/{r.output_tokens}"
            )
            if r.priority_score is not None:
                line += f" priority={r.priority_score}({r.priority_level})"
            errors = ", ".join(e.get("code", "?") for e in (r.errors or []))
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


def cmd_usage(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        ledger = SqlUsageLedger(db)
        if not ledger.check_schema():
            print(SchemaNotReady.safe_message, file=sys.stderr)
            return 2
        snap = ledger.snapshot(settings.ai_model, datetime.now(UTC))
        print(
            json.dumps(
                snap.as_dict(requests_per_day=settings.ai_requests_per_day, tokens_per_day=settings.ai_tokens_per_day),
                indent=2,
            )
        )
        return 0
    finally:
        db.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Print JSON.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Show configuration.")
    check.add_argument("--ping", action="store_true", help="Confirm AI_MODEL exists for this key (no quota used).")
    run = sub.add_parser("run", help="Analyse clusters that need it.", parents=[common])
    run.add_argument("--retry-failed", action="store_true", help="Retry failed/invalid stories now.")
    run.add_argument("--limit", type=int, default=None, help="At most this many stories (<= AI_MAX_STORIES_PER_RUN).")
    dry = sub.add_parser("dry-run", help="Analyse current clusters; store no analyses.", parents=[common])
    dry.add_argument("--limit", type=int, default=2)
    lst = sub.add_parser("list", help="Print stored analyses.", parents=[common])
    lst.add_argument("--limit", type=int, default=20)
    rec = sub.add_parser("records", help="Per-story status and errors.")
    rec.add_argument("--limit", type=int, default=50)
    sub.add_parser("usage", help="Today's Gemini usage.")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {
        "check": cmd_check,
        "run": cmd_run,
        "dry-run": cmd_dry_run,
        "list": cmd_list,
        "records": cmd_records,
        "usage": cmd_usage,
    }
    try:
        return commands[args.command](settings, args)
    except IntelligenceConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    from scripts._cli import guarded

    sys.exit(guarded(main, "AI analysis"))
