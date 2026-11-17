"""Entry points for other stages. Stage 3.4 reads its input with `list_story_analyses`."""

from datetime import datetime

from app.core.config import Settings
from app.db.session import Database
from app.intelligence.contract import StoryAnalysis
from app.intelligence.quota import SqlUsageLedger
from app.intelligence.runner import AnalysisReport, AnalysisRunner
from app.intelligence.store import SqlAnalysisStore
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore


def build_runner(settings: Settings, db: Database, **kwargs) -> AnalysisRunner:
    """Stage 3.2 clusters (read-only, built on read from ingestion_items) → Stage 3.3 runner over Supabase."""
    processing = ProcessingRunner(settings, SqlProcessingStore(db))
    kwargs.setdefault("ledger", SqlUsageLedger(db))
    return AnalysisRunner(
        settings, SqlAnalysisStore(db), lambda: processing.clusters(limit=settings.processing_max_items), **kwargs
    )


def run_analysis(settings: Settings, db: Database, *, retry_failed: bool = False) -> AnalysisReport:
    return build_runner(settings, db).run(retry_failed=retry_failed)


def list_story_analyses(
    settings: Settings,
    db: Database,
    *,
    statuses: list[str] | None = None,
    limit: int = 100,
    updated_since: datetime | None = None,
) -> list[StoryAnalysis]:
    """Validated story analyses, most recently updated first. Read-only.

    Default statuses: completed and needs_review (check `review_flags` before using a needs_review story).
    Every generated text field is AI-generated and unreviewed; priority is preliminary (see contract.py)."""
    runner = AnalysisRunner(settings, SqlAnalysisStore(db), list)
    return runner.list(statuses=statuses, limit=limit, updated_since=updated_since)
