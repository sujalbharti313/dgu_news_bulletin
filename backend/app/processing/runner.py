"""Stage 3.2 orchestration: lock, load staged items, decide, save, report. Nothing is published.

A run is recorded as one `ingestion_jobs` row (job_type `story_processing`) with its counts. Only one run
can hold the lock at a time across processes; a second one is reported as skipped. Logs carry counts
only, never titles, excerpts or URLs.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config import Settings
from app.ingestion.errors import SourceConfigError
from app.ingestion.sources import load_sources
from app.models.enums import IngestionJobStatus
from app.processing.clusters import build_clusters
from app.processing.contract import StoryCluster
from app.processing.models import ProcessingReport, SourceInfo, Thresholds
from app.processing.pipeline import process_items
from app.processing.store import ProcessingStore, SchemaNotReady

logger = logging.getLogger("app.processing")


def load_source_info(settings: Settings) -> dict[str, SourceInfo]:
    """Name and category of each configured source, used to label source references. Never fatal."""
    try:
        return {
            s.key: SourceInfo(name=s.name, category=s.category) for s in load_sources(settings.ingestion_sources_file)
        }
    except SourceConfigError as exc:
        logger.warning("source configuration unavailable for labelling: %s", exc.safe_message)
        return {}


class ProcessingRunner:
    def __init__(
        self,
        settings: Settings,
        store: ProcessingStore,
        *,
        sources: dict[str, SourceInfo] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        dry_run: bool = False,
    ):
        self.settings = settings
        self.store = store
        self.thresholds = Thresholds.from_settings(settings)
        self.sources = sources if sources is not None else load_source_info(settings)
        self.clock = clock
        self.dry_run = dry_run

    def run(self) -> ProcessingReport:
        now = self.clock()
        report = ProcessingReport(started_at=now, dry_run=self.dry_run)
        try:
            self.store.check_schema()
        except SchemaNotReady as exc:
            report.status, report.reason, report.finished_at = "failed", exc.safe_message, self.clock()
            logger.error("processing not started: %s", exc.safe_message)
            return report
        recovered = self.store.recover_stale(now, self.settings.ingestion_job_stale_minutes)
        if recovered:
            logger.warning("marked %d abandoned job(s) as failed", recovered)
        job_id = self.store.start(now)
        if job_id is None:
            report.status, report.reason, report.finished_at = (
                "skipped",
                "already running in another process",
                self.clock(),
            )
            logger.info("processing skipped: already running")
            return report
        report.job_id = str(job_id)
        try:
            items = self.store.load_items(now, self.settings.processing_max_items)
            result = process_items(items, thresholds=self.thresholds, now=now)
            result.counts["written"] = self.store.apply(result.updates)
            report.counts = result.counts
            self.store.finish(
                job_id,
                status=IngestionJobStatus.SUCCEEDED,
                now=self.clock(),
                details={"counts": result.counts, "thresholds": self.thresholds.as_dict()},
            )
            logger.info(
                "processing succeeded: %s", result.counts, extra={"outcome": "succeeded", "counts": result.counts}
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the scheduler/worker
            report.status = "failed"
            report.reason = f"Unexpected error ({type(exc).__name__}). See server logs."
            logger.exception("processing failed", extra={"outcome": "failed"})
            try:
                self.store.finish(
                    job_id,
                    status=IngestionJobStatus.FAILED,
                    now=self.clock(),
                    details={"error_type": type(exc).__name__},
                    error=report.reason,
                )
            except Exception:  # noqa: BLE001 - the stale-job sweep closes it later
                logger.exception("could not record processing failure")
        report.finished_at = self.clock()
        return report

    def clusters(self, *, limit: int = 100, updated_since: datetime | None = None) -> list[StoryCluster]:
        """Current story clusters (the Stage 3.3 input), most recently active first."""
        self.store.check_schema()
        now = self.clock()
        items = self.store.load_items(now, self.settings.processing_max_items)
        clusters = build_clusters(items, sources=self.sources, thresholds=self.thresholds, now=now)
        if updated_since is not None:
            clusters = [c for c in clusters if c.processing.processed_at >= updated_since]
        return clusters[:limit]
