"""Entry points for other stages. Stage 3.4 reads its input with `list_story_intelligence`."""

from datetime import datetime

from app.core.config import Settings
from app.db.session import Database
from app.intelligence.contract import StoryIntelligence
from app.intelligence.runner import IntelligenceReport, IntelligenceRunner
from app.intelligence.store import SqlIntelligenceStore
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore


def build_runner(settings: Settings, db: Database, **kwargs) -> IntelligenceRunner:
    """Stage 3.2 clusters (read-only, built on read from ingestion_items) → Stage 3.3 runner over Supabase."""
    processing = ProcessingRunner(settings, SqlProcessingStore(db))
    return IntelligenceRunner(
        settings,
        SqlIntelligenceStore(db),
        lambda: processing.clusters(limit=settings.processing_max_items),
        **kwargs,
    )


def run_intelligence(settings: Settings, db: Database, *, retry_failed: bool = False) -> IntelligenceReport:
    return build_runner(settings, db).run(retry_failed=retry_failed)


def list_story_intelligence(
    settings: Settings,
    db: Database,
    *,
    statuses: list[str] | None = None,
    limit: int = 100,
    updated_since: datetime | None = None,
) -> list[StoryIntelligence]:
    """Validated story intelligence, most recently updated first. Read-only.

    Default statuses: analysed and needs_review (check `review_flags` before using a needs_review story).
    Every generated text field is AI-generated and unreviewed (see contract.py)."""
    store = SqlIntelligenceStore(db)
    return IntelligenceRunner(settings, store, list).list(statuses=statuses, limit=limit, updated_since=updated_since)
