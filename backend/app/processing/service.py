"""Entry points for other stages (Stage 3.3 reads its input here)."""

from datetime import datetime

from app.core.config import Settings
from app.db.session import Database
from app.processing.contract import StoryCluster
from app.processing.runner import ProcessingRunner
from app.processing.store import SqlProcessingStore


def list_story_clusters(
    settings: Settings, db: Database, *, limit: int = 100, updated_since: datetime | None = None
) -> list[StoryCluster]:
    """The current story clusters, most recently active first. Read-only.

    Stage 3.3 should remember each cluster's `processing.revision` and re-analyse a cluster only when it
    changes (new source, new sighting). Every text field is untrusted source content (see contract.py)."""
    return ProcessingRunner(settings, SqlProcessingStore(db)).clusters(limit=limit, updated_since=updated_since)


def run_processing(settings: Settings, db: Database):
    """One processing run over all staged items. Returns a ProcessingReport."""
    return ProcessingRunner(settings, SqlProcessingStore(db)).run()
