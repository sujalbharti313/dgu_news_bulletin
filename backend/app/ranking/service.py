"""Entry points for other stages. Stage 3.5 reads the shortlist with `latest_ranking`."""

from datetime import UTC, datetime

from app.core.config import Settings
from app.db.session import Database
from app.intelligence.quota import SqlUsageLedger
from app.intelligence.store import SqlAnalysisStore
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore
from app.ranking.contract import RankingResult
from app.ranking.runner import RankingReport, RankingRunner
from app.ranking.store import SqlRankingStore


def build_runner(settings: Settings, db: Database, **kwargs) -> RankingRunner:
    """Stage 3.2 clusters (read-only) + Stage 3.3 analyses (read-only) → Stage 3.4 runner over Supabase."""
    processing = ProcessingRunner(settings, SqlProcessingStore(db))
    analyses = SqlAnalysisStore(db)
    kwargs.setdefault("ledger", SqlUsageLedger(db))
    return RankingRunner(
        settings,
        SqlRankingStore(db),
        lambda: processing.clusters(limit=settings.processing_max_items),
        lambda: analyses.list(now=kwargs.get("clock", _now)(), statuses=None, limit=settings.processing_max_items),
        **kwargs,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def run_ranking(
    settings: Settings, db: Database, *, week: str = "previous", retry_failed: bool = False
) -> RankingReport:
    return build_runner(settings, db).run(week=week, retry_failed=retry_failed)


def latest_ranking(
    settings: Settings, db: Database, *, week: str | None = None, include_incomplete: bool = False
) -> RankingResult | None:
    """The latest stored ranking, by default only a COMPLETED one (an incomplete run is not a final ranking).
    `week`: None = any edition, 'previous', 'current' or a date inside the wanted week. Read-only."""
    runner = RankingRunner(settings, SqlRankingStore(db), list, list)
    return runner.latest(week=week, include_incomplete=include_incomplete)
