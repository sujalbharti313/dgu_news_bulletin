"""Stage 3.4 verification and editorial ranking: verify Stage 3.3 analyses against their sources, score them,
apply quality gates and build the diversity-aware weekly shortlist for Stage 3.5.

    python -m scripts.rank check                         # configuration, weights, gates, registry (no DB, no calls)
    python -m scripts.rank dry-run --week current        # rank this week so far; store NOTHING
    python -m scripts.rank dry-run --week current --no-ai   # same, rules only: no Gemini requests at all
    python -m scripts.rank run                           # rank last Monday-Sunday week and store it (migration 0006)
    python -m scripts.rank run --week 2026-11-10         # the week containing that date
    python -m scripts.rank run --retry-failed            # retry failed AI checks now
    python -m scripts.rank show                          # the latest stored ranking (shortlist + reasons)
    python -m scripts.rank show --all --json             # every evaluated story, as JSON
    python -m scripts.rank runs                          # recent ranking runs and their status

Weeks run Monday 00:00 to Sunday 23:59 Asia/Kolkata. `--week previous` (default) is the last complete week.
With RANKING_AI_CHECK=true, `run` and `dry-run` send at most RANKING_AI_MAX_CHECKS_PER_RUN Gemini requests
(plus repairs), counted in the same free-tier daily ledger as Stage 3.3; checks are cached, so a re-run on
unchanged stories sends none. Nothing is published. Exit codes: 0 completed, 1 incomplete or failed, 2 config.
"""

import argparse
import json
import sys
from datetime import UTC, datetime

from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.intelligence.quota import MemoryUsageLedger, SqlUsageLedger
from app.intelligence.store import SqlAnalysisStore
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore
from app.ranking.config import RankingConfigError, load_ranking_config, load_source_registry
from app.ranking.contract import RankingResult
from app.ranking.runner import RankingRunner
from app.ranking.service import build_runner
from app.ranking.store import MemoryRankingStore, SchemaNotReady, SqlRankingStore


def _database(settings: Settings) -> Database | None:
    if settings.database_url is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return None
    return Database(settings.model_copy(update={"db_use_null_pool": True}))


def print_result(result: RankingResult, *, show_all: bool, as_json: bool) -> None:
    if as_json:
        data = result.model_dump(mode="json", by_alias=True)
        if not show_all:
            data.pop("candidates", None)
        print(json.dumps(data, indent=2))
        return
    e = result.edition
    print(f"Edition {e['startDate']} to {e['endDate']} ({e['timezone']}): ranking {result.status.upper()}")
    if result.status_reason:
        print(f"  {result.status_reason}")
    t = result.totals
    print(
        f"  {t.clusters_in_window} stories collected · {t.evaluated} evaluated · {t.eligible} eligible · "
        f"{t.review_required} need review · {t.ineligible} ineligible · {t.pending} pending · {t.failed} failed"
    )
    print(f"  config {result.ranking_config_version} · verification {result.verification_config_version}")
    ai = result.ai_check
    if ai.get("enabled"):
        print(f"  AI check ({ai.get('model')}): {ai.get('statuses')} · calls this run: {ai.get('calls', 0)}")
    else:
        print("  AI check: off (rules only)")
    for note in result.notes:
        print(f"  note: {note}")
    print(f"\nShortlist ({len(result.shortlist)}):")
    for c in result.shortlist:
        _print_candidate(c)
    if show_all:
        rest = [c for c in result.candidates if not c.shortlisted]
        print(f"\nOther evaluated stories ({len(rest)}):")
        for c in rest:
            _print_candidate(c)
    for p in result.pending:
        print(f"  pending {p.story_cluster_id}: {p.reason} ({p.detail})")
    for f in result.failed:
        print(f"  FAILED {f.story_cluster_id}: {f.error}")


def _print_candidate(c) -> None:
    s = c.final_scores
    where = f"#{c.shortlist_rank}" if c.shortlisted else f"rank {c.rank}"
    print(
        f"\n {where:>7}  {s.final_priority_score:5.1f}  {c.priority_classification:<11} "
        f"[{c.eligibility_status}] {c.headline}"
    )
    print(
        f"          impact {s.global_impact} · technical {s.technical_significance} · students {s.student_relevance} · "
        f"evidence {s.evidence_quality} · credibility {s.source_credibility} · timeliness {s.timeliness}"
    )
    print(f"          {c.ranking_explanation}")
    if c.review_flags:
        print(f"          flags: {', '.join(c.review_flags[:10])}")


def cmd_check(settings: Settings, args) -> int:
    try:
        config = load_ranking_config(settings.ranking_config_file)
        registry = load_source_registry(settings.source_registry_file)
    except RankingConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    print(f"Ranking config: {settings.ranking_config_file} version {config.ranking_version}")
    print("Weights: " + ", ".join(f"{k} {v:.0%}" for k, v in config.weights.as_dict().items()))
    c = config.classification
    print(
        f"Classes: Exceptional >= {c.exceptional}, High >= {c.high}, Medium >= {c.medium}, Low >= {c.low}, "
        "else Very Low"
    )
    gates = [name for name, g in config.gates if g.enabled]
    print(f"Quality gates on: {', '.join(gates)}")
    sl = config.shortlist
    print(
        f"Shortlist: target {sl.target}, minimum score {sl.min_score}, at most {sl.max_per_category} per category and "
        f"{sl.max_per_organisation} per organisation (exceptional >= {sl.exceptional_score} exempt)"
    )
    reviewed = sum(1 for e in registry.sources if e.reviewed_on)
    print(f"Source registry: {len(registry.sources)} entries ({reviewed} reviewed), version {registry.version}")
    print(
        f"AI consistency check (RANKING_AI_CHECK): {'on' if settings.ranking_ai_check else 'off (rules only)'}; "
        f"model {settings.ai_model}; at most {settings.ranking_ai_max_checks_per_run} checks/run; "
        f"stories scoring >= {config.ai_check.min_score}"
    )
    print(f"API key (GEMINI_API_KEY): {'configured' if settings.gemini_api_key else 'NOT SET'}")
    print(f"Database: {describe_database_target(settings)}")
    if settings.ranking_ai_check and settings.gemini_api_key is None:
        print("Not ready: set GEMINI_API_KEY, or RANKING_AI_CHECK=false for rules only.", file=sys.stderr)
        return 2
    print("Ready.")
    return 0


def _finish(report, args) -> int:
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        if report.result is not None:
            print_result(report.result, show_all=args.all, as_json=False)
        else:
            print(f"Ranking run {report.status}: {report.reason}")
        for alert in report.alerts:
            print(f"  ALERT: {alert['message']}")
        if report.reused_run:
            print("  (inputs unchanged since an earlier run: the same ranking record was updated)")
    if report.status == "failed" and report.job_id is None:
        return 2
    return 0 if report.status in ("completed", "skipped") else 1


def cmd_run(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        print(f"Ranking stories in {describe_database_target(settings)}", file=sys.stderr)
        runner = build_runner(settings, db)
        report = runner.run(week=args.week, retry_failed=args.retry_failed, ai_check=False if args.no_ai else None)
        return _finish(report, args)
    finally:
        db.dispose()


def cmd_dry_run(settings: Settings, args) -> int:
    """Reads clusters and analyses from the database; stores nothing (AI checks, if on, are real requests and are
    counted in the daily ledger when migration 0005 is applied)."""
    db = _database(settings)
    if db is None:
        return 2
    try:
        processing = ProcessingRunner(settings, SqlProcessingStore(db))
        analyses = SqlAnalysisStore(db)
        analyses.check_schema()
        ledger = SqlUsageLedger(db)
        if not ledger.check_schema():
            ledger = MemoryUsageLedger()
        sql_store = SqlRankingStore(db)
        memory = MemoryRankingStore()
        try:  # reuse cached AI checks from earlier stored runs, if migration 0006 is applied
            sql_store.check_schema()
            memory.load_verifications = sql_store.load_verifications
        except SchemaNotReady:
            print("Migration 0006 not applied yet: nothing cached, nothing stored.", file=sys.stderr)
        runner = RankingRunner(
            settings,
            memory,
            lambda: processing.clusters(limit=settings.processing_max_items),
            lambda: analyses.list(now=datetime.now(UTC), statuses=None, limit=settings.processing_max_items),
            ledger=ledger,
            dry_run=True,
        )
        report = runner.run(week=args.week, ai_check=False if args.no_ai else None)
        print("(dry run: nothing stored)", file=sys.stderr)
        return _finish(report, args)
    except SchemaNotReady as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    finally:
        db.dispose()


def cmd_show(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        runner = RankingRunner(settings, SqlRankingStore(db), list, list)
        result = runner.latest(week=args.week, include_incomplete=True)
        if result is None:
            print("No stored ranking yet. Run: python -m scripts.rank run")
            return 1
        print_result(result, show_all=args.all, as_json=args.json)
        return 0
    except SchemaNotReady as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    finally:
        db.dispose()


def cmd_runs(settings: Settings, args) -> int:
    db = _database(settings)
    if db is None:
        return 2
    try:
        store = SqlRankingStore(db)
        store.check_schema()
        runs = store.list_runs(limit=args.limit)
        print(f"{len(runs)} ranking run(s)")
        for r in runs:
            print(
                f"  {r.processed_at:%Y-%m-%d %H:%M} UTC  week {r.edition_start}  {r.status:<10} "
                f"eligible {r.total_eligible}  review {r.total_review_required}  pending {r.total_pending}  "
                f"shortlist {len(r.shortlist_cluster_ids)}  {r.id}"
            )
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
    common.add_argument("--all", action="store_true", help="Also print stories that were not shortlisted.")
    week = argparse.ArgumentParser(add_help=False)
    week.add_argument("--week", default="previous", help="previous (default), current, or a date YYYY-MM-DD.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Show and validate the configuration.")
    run = sub.add_parser("run", help="Rank an edition week and store it.", parents=[common, week])
    run.add_argument("--retry-failed", action="store_true", help="Retry failed AI checks now.")
    run.add_argument("--no-ai", action="store_true", help="Rules only for this run (no Gemini requests).")
    dry = sub.add_parser("dry-run", help="Rank an edition week; store nothing.", parents=[common, week])
    dry.add_argument("--no-ai", action="store_true", help="Rules only (no Gemini requests).")
    show = sub.add_parser("show", help="Print the latest stored ranking.", parents=[common])
    show.add_argument("--week", default=None, help="previous, current or a date; default: the latest of any week.")
    runs = sub.add_parser("runs", help="List recent ranking runs.")
    runs.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {"check": cmd_check, "run": cmd_run, "dry-run": cmd_dry_run, "show": cmd_show, "runs": cmd_runs}
    try:
        return commands[args.command](settings, args)
    except RankingConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    except ValueError as exc:  # a bad --week value
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
