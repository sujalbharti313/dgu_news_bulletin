"""Ingestion orchestration: every configured source runs as its own isolated job.

For each source the runner decides succeeded / skipped / failed, records it in ingestion_jobs, and moves
on. An exception in one source (network, parse, auth, even a bug) is caught, summarised safely and
recorded; it never stops the other sources. Nothing here publishes: items are staged as `pending`.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.ingestion.errors import IngestionError
from app.ingestion.http import SafeHttpClient
from app.ingestion.models import CollectResult, NormalizedItem, RunReport, SourceOutcome, SourceReport
from app.ingestion.normalize import InvalidItem, normalize_item
from app.ingestion.providers.gmail import GmailClient, collect_gmail
from app.ingestion.providers.rss import collect_rss
from app.ingestion.sources import GmailSourceConfig, RssSourceConfig, load_sources
from app.ingestion.store import IngestionStore
from app.models.enums import IngestionJobStatus, IngestionJobType, IngestionSourceType

logger = logging.getLogger("app.ingestion")

AnySource = RssSourceConfig | GmailSourceConfig
HttpFactory = Callable[[], SafeHttpClient]


def _job_type(source: AnySource) -> str:
    return IngestionJobType.NEWSLETTER_IMPORT if source.type == "gmail" else IngestionJobType.SOURCE_FETCH


def _safe_error(exc: BaseException) -> str:
    """Never the exception's own text for unexpected errors: it may contain URLs, tokens or content."""
    if isinstance(exc, IngestionError):
        return exc.safe_message
    return f"Unexpected error ({type(exc).__name__}). See server logs."


class IngestionRunner:
    def __init__(
        self,
        settings: Settings,
        store: IngestionStore,
        *,
        sources: list[AnySource] | None = None,
        http_factory: HttpFactory | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        dry_run: bool = False,
    ):
        self.settings = settings
        self.store = store
        self.sources = sources if sources is not None else load_sources(settings.ingestion_sources_file)
        self.http_factory = http_factory or (lambda: SafeHttpClient.from_settings(settings))
        self.clock = clock
        self.dry_run = dry_run
        # Dry runs keep the normalised items here (per source key) instead of storing them.
        self.dry_run_items: dict[str, list[NormalizedItem]] = {}

    def select(self, keys: list[str] | None) -> list[AnySource]:
        if not keys:
            return list(self.sources)
        known = {s.key: s for s in self.sources}
        unknown = sorted(set(keys) - known.keys())
        if unknown:
            raise ValueError(f"Unknown source key(s): {', '.join(unknown)}")
        return [known[k] for k in dict.fromkeys(keys)]

    def run(self, keys: list[str] | None = None) -> RunReport:
        selected = self.select(keys)
        now = self.clock()
        report = RunReport(started_at=now, dry_run=self.dry_run)
        if not self.dry_run:
            # Housekeeping first, so retention holds even when every source later fails.
            report.purged_items = self.store.purge_expired_items(now)
            report.purged_jobs = self.store.purge_old_jobs(now, self.settings.ingestion_job_retention_days)
            recovered = self.store.recover_stale_jobs(now, self.settings.ingestion_job_stale_minutes)
            if recovered:
                logger.warning("marked %d abandoned ingestion job(s) as failed", recovered)
        logger.info("ingestion run started: %d source(s)%s", len(selected), " (dry run)" if self.dry_run else "")
        for source in selected:
            report.sources.append(self._run_source(source))
        report.finished_at = self.clock()
        logger.info(
            "ingestion run finished: %s",
            report.status,
            extra={"outcome": report.status, "counts": {s.key: s.outcome.value for s in report.sources}},
        )
        return report

    # -- one source -------------------------------------------------------------------------------------

    def _skip(self, source: AnySource, reason: str, *, record: bool) -> SourceReport:
        if record and not self.dry_run:
            self.store.record_skipped(source.key, _job_type(source), reason, self.clock())
        logger.info("source %s skipped: %s", source.key, reason, extra={"source_key": source.key, "outcome": "skipped"})
        return SourceReport(source.key, source.type, SourceOutcome.SKIPPED, reason=reason)

    def _run_source(self, source: AnySource) -> SourceReport:
        if not source.enabled:
            return self._skip(source, "disabled in configuration", record=False)
        if source.type == "gmail" and not self.settings.gmail_configured:
            return self._skip(
                source,
                "Gmail is not configured (GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN)",
                record=True,
            )

        started = time.perf_counter()
        job_id = None
        if not self.dry_run:
            job_id = self.store.start_job(source.key, _job_type(source), self.clock())
            if job_id is None:
                return self._skip(source, "already running in another process", record=True)

        http = None
        try:
            http = self.http_factory()
            cursor = None if self.dry_run else self.store.last_cursor(source.key)
            result = self._collect(source, http, cursor)
            items, invalid = self._normalize(source, result)
            counts = {
                **result.counts,
                "candidates": len(result.items),
                "invalid": invalid,
                "not_modified": int(result.not_modified),
            }
            if self.dry_run:
                counts.update(inserted=0, duplicates=0, already_in_bulletin=0, would_stage=len(items))
                self.dry_run_items[source.key] = items
            else:
                expires = self.clock() + timedelta(days=self.settings.ingestion_item_retention_days)
                saved = self.store.save_items(items, job_id, expires)
                counts.update(
                    inserted=saved.inserted, duplicates=saved.duplicates, already_in_bulletin=saved.already_in_bulletin
                )
                self.store.finish_job(
                    job_id,
                    status=IngestionJobStatus.SUCCEEDED,
                    now=self.clock(),
                    attempts=http.attempts,
                    details={"counts": counts, "cursor": result.cursor, "warnings": result.warnings[:10]},
                )
            for warning in result.warnings[:10]:
                logger.warning("source %s warning: %s", source.key, warning, extra={"source_key": source.key})
            logger.info(
                "source %s succeeded: %s",
                source.key,
                counts,
                extra={"source_key": source.key, "outcome": "succeeded", "counts": counts},
            )
            return SourceReport(
                source.key,
                source.type,
                SourceOutcome.SUCCEEDED,
                counts=counts,
                duration_ms=(time.perf_counter() - started) * 1000,
                job_id=str(job_id) if job_id else None,
                reason="; ".join(result.warnings[:3]) or None,
            )
        except Exception as exc:  # noqa: BLE001 - one source must never break the run
            message = _safe_error(exc)
            if isinstance(exc, IngestionError):
                logger.warning(
                    "source %s failed: %s", source.key, message, extra={"source_key": source.key, "outcome": "failed"}
                )
            else:
                logger.exception(
                    "source %s failed unexpectedly", source.key, extra={"source_key": source.key, "outcome": "failed"}
                )
            if job_id is not None:
                try:
                    self.store.finish_job(
                        job_id,
                        status=IngestionJobStatus.FAILED,
                        now=self.clock(),
                        attempts=http.attempts if http else 0,
                        details={"error_type": type(exc).__name__},
                        error=message,
                    )
                except Exception:  # noqa: BLE001 - the stale-job sweep will close it later
                    logger.exception("could not record failure", extra={"source_key": source.key})
            return SourceReport(
                source.key,
                source.type,
                SourceOutcome.FAILED,
                reason=message,
                duration_ms=(time.perf_counter() - started) * 1000,
                job_id=str(job_id) if job_id else None,
            )
        finally:
            if http is not None:
                http.close()

    def _collect(self, source: AnySource, http: SafeHttpClient, cursor: dict | None) -> CollectResult:
        max_items = min(
            source.max_items or self.settings.ingestion_max_items_per_source,
            self.settings.ingestion_max_items_per_source,
        )
        if isinstance(source, RssSourceConfig):
            return collect_rss(source, http, cursor, max_items=max_items, now=self.clock())
        client = GmailClient(
            http,
            client_id=self.settings.gmail_client_id or "",
            client_secret=self.settings.gmail_client_secret.get_secret_value(),
            refresh_token=self.settings.gmail_refresh_token.get_secret_value(),
        )
        return collect_gmail(source, client, cursor, now=self.clock(), max_items=max_items)

    def _normalize(self, source: AnySource, result: CollectResult) -> tuple[list[NormalizedItem], int]:
        source_type = IngestionSourceType(source.type)
        collected_at = self.clock()
        items, invalid = [], 0
        for raw in result.items:
            try:
                items.append(
                    normalize_item(raw, source_key=source.key, source_type=source_type, collected_at=collected_at)
                )
            except InvalidItem:
                invalid += 1
        return items, invalid
