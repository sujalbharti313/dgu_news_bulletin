"""Stage 3.4 semantic verification and editorial ranking: embed Stage 3.3 stories (Gemini embeddings, stored in
pgvector), retrieve related stories, run one Gemini editorial evaluation per story, verify, score, gate and build
the diversity-aware weekly shortlist for Stage 3.5.

    python -m scripts.rank check                         # configuration, models, weights, gates (no DB, no calls)
    python -m scripts.rank dry-run --week current        # rank this week so far; ranking results are NOT stored
    python -m scripts.rank run                           # rank last Monday-Sunday week and store it (migration 0006)
    python -m scripts.rank run --week 2026-11-10         # the week containing that date
    python -m scripts.rank run --retry-failed            # retry failed embeddings/evaluations now
    python -m scripts.rank similar <cluster-id>          # nearest stored stories, to tune the similarity thresholds
    python -m scripts.rank show                          # the latest stored ranking (shortlist + reasons)
    python -m scripts.rank show --all --json             # every evaluated story, as JSON
    python -m scripts.rank runs                          # recent ranking runs and their status

Weeks run Monday 00:00 to Sunday 23:59 Asia/Kolkata. `--week previous` (default) is the last complete week.
Gemini requests (free tier only): one embedding per new or changed story (EMBEDDING_MODEL, batched, cached by
content hash) and at most RANKING_MAX_EVALUATIONS_PER_RUN evaluations (AI_MODEL, plus repairs), all counted in the
same daily ledger as Stage 3.3. A re-run on unchanged stories sends none. When a free quota is used up the run
stops asking, leaves the rest pending and resumes on a later run. A dry run stores embeddings (a cache) but no
ranking results. Nothing is published. Exit codes: 0 completed, 1 incomplete or failed, 2 config.
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, describe_database_target
from app.core.logging import configure_logging
from app.db.session import Database
from app.intelligence.quota import MemoryUsageLedger, SqlUsageLedger
from app.intelligence.store import SqlAnalysisStore
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore
from app.ranking.config import RankingConfigError, load_ranking_config, load_source_registry
from app.ranking.contract import RankingResult
from app.ranking.embeddings import EMBEDDING_INPUT_VERSION
from app.ranking.runner import RankingRunner
from app.ranking.service import build_runner
from app.ranking.store import DryRunStore, SchemaNotReady, SqlRankingStore


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
    ai = result.ai
    emb, ev = ai.get("embeddings") or {}, ai.get("evaluations") or {}
    print(
        f"  embeddings ({ai.get('embeddingModel')}, {ai.get('embeddingDimensions')} dims): "
        f"{ai.get('embeddingStatuses')} · new this run {emb.get('embedded', 0)} in {emb.get('calls', 0)} call(s)"
    )
    print(
        f"  evaluations ({ai.get('evaluationModel')}): {ai.get('evaluationStatuses')} · "
        f"new this run {ev.get('evaluated', 0)} in {ev.get('calls', 0)} call(s)"
    )
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
    print(
        f"  also at most {sl.max_per_development_type} per development type and {sl.max_per_topic} per semantic topic"
    )
    reviewed = sum(1 for e in registry.sources if e.reviewed_on)
    print(f"Source registry: {len(registry.sources)} entries ({reviewed} reviewed), version {registry.version}")
    sem = config.semantic
    print(
        f"Embeddings: {settings.embedding_model}, {settings.embedding_dimensions} dims, batches of "
        f"{settings.embedding_batch_size}, at most {settings.embedding_requests_per_minute}/min and "
        f"{settings.embedding_requests_per_day}/day (free tier)"
    )
    print(
        f"Retrieval: up to {sem.retrieval_limit} related stories with similarity >= {sem.related_threshold}, "
        f"looking back {sem.lookback_days} days; same event needs the evaluation's judgement AND similarity >= "
        f"{sem.same_event_min_similarity}; same topic >= {sem.topic_similarity}"
    )
    print(
        f"Editorial evaluation: {settings.ai_model}, at most {settings.ranking_max_evaluations_per_run} per run, "
        f"{settings.ranking_max_attempts} attempts per story"
    )
    print(f"API key (GEMINI_API_KEY): {'configured' if settings.gemini_api_key else 'NOT SET'}")
    print(f"Database: {describe_database_target(settings)}")
    if settings.gemini_api_key is None:
        print("Not ready: set GEMINI_API_KEY (free key from Google AI Studio).", file=sys.stderr)
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
        report = runner.run(week=args.week, retry_failed=args.retry_failed)
        return _finish(report, args)
    finally:
        db.dispose()


def cmd_dry_run(settings: Settings, args) -> int:
    """Reads clusters, analyses, earlier results and embeddings from the database. Ranking results stay in memory;
    new embeddings are stored (a cache), so the real run does not spend the free quota on them again."""
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
        runner = RankingRunner(
            settings,
            DryRunStore(SqlRankingStore(db)),
            lambda: processing.clusters(limit=settings.processing_max_items),
            lambda: analyses.list(now=datetime.now(UTC), statuses=None, limit=settings.processing_max_items),
            ledger=ledger,
            dry_run=True,
        )
        report = runner.run(week=args.week, retry_failed=args.retry_failed)
        print("(dry run: no ranking results stored)", file=sys.stderr)
        return _finish(report, args)
    except SchemaNotReady as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    finally:
        db.dispose()


def cmd_similar(settings: Settings, args) -> int:
    """The nearest stored stories to one story (read-only, no Gemini calls): for tuning [semantic] thresholds."""
    try:
        cluster_id = uuid.UUID(args.cluster_id)
    except ValueError:
        print("cluster id must be a UUID", file=sys.stderr)
        return 2
    db = _database(settings)
    if db is None:
        return 2
    try:
        store = SqlRankingStore(db)
        store.check_schema(settings.embedding_dimensions)
        if cluster_id not in store.load_embeddings([cluster_id]):
            print("No embedding stored for this story yet (run `python -m scripts.rank run` first).")
            return 1
        far = datetime(1970, 1, 1, tzinfo=UTC)
        hits = store.similar(
            cluster_id,
            model=settings.embedding_model,
            version=EMBEDDING_INPUT_VERSION,
            window_start=far,
            window_end=datetime.now(UTC) + timedelta(days=1),
            min_similarity=args.min,
            limit=args.limit,
        )
        config = load_ranking_config(settings.ranking_config_file).semantic
        print(
            f"{len(hits)} stored stories with similarity >= {args.min} (related >= {config.related_threshold}, "
            f"same event >= {config.same_event_min_similarity}, same topic >= {config.topic_similarity})"
        )
        for h in hits:
            print(f"  {h.similarity:.4f}  {h.cluster_id}  collected {h.first_collected_at:%Y-%m-%d}")
        return 0
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
        store.check_schema(settings.embedding_dimensions)
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
    run.add_argument("--retry-failed", action="store_true", help="Retry failed embeddings/evaluations now.")
    dry = sub.add_parser("dry-run", help="Rank an edition week; store no ranking results.", parents=[common, week])
    dry.add_argument("--retry-failed", action="store_true", help="Retry failed embeddings/evaluations now.")
    sim = sub.add_parser("similar", help="Nearest stored stories to one story (threshold tuning).")
    sim.add_argument("cluster_id")
    sim.add_argument("--min", type=float, default=0.5, help="Minimum cosine similarity (default 0.5).")
    sim.add_argument("--limit", type=int, default=10)
    show = sub.add_parser("show", help="Print the latest stored ranking.", parents=[common])
    show.add_argument("--week", default=None, help="previous, current or a date; default: the latest of any week.")
    runs = sub.add_parser("runs", help="List recent ranking runs.")
    runs.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    commands = {
        "check": cmd_check,
        "run": cmd_run,
        "dry-run": cmd_dry_run,
        "similar": cmd_similar,
        "show": cmd_show,
        "runs": cmd_runs,
    }
    try:
        return commands[args.command](settings, args)
    except RankingConfigError as exc:
        print(exc.safe_message, file=sys.stderr)
        return 2
    except ValueError as exc:  # a bad --week value
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    from scripts._cli import guarded

    sys.exit(guarded(main, "Ranking"))
