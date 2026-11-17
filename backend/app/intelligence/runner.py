"""Stage 3.3 orchestration: lock, choose which clusters need analysis, analyse them concurrently (bounded),
save each result as it finishes, report. Nothing is verified, ranked, selected or published here.

Idempotency and duplicate-request prevention: a cluster is sent to Gemini again only when its Stage 3.2
revision or the engine key changes, or when an earlier attempt failed and its retry time has come (at most
AI_MAX_ATTEMPTS_PER_STORY attempts, unless the run is asked to retry failures). One run at a time across all
processes (job lock), and each cluster at most once per run.

Quota exhaustion (Google's daily free quota, or the daily limits configured here): the run stops sending
requests, stories that were not analysed are marked `quota_limited` (not counted as an attempt, retried
automatically after the reset) and an admin alert is logged and stored on the run record. Logs carry ids and
counts only, never source text.
"""

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.intelligence.categories import CategorySet, load_categories
from app.intelligence.contract import StoryAnalysis
from app.intelligence.engine import AnalysisOutcome, StoryAnalyzer, engine_key
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers import build_provider
from app.intelligence.providers.base import IntelligenceProvider
from app.intelligence.providers.gemini import MISSING_KEY
from app.intelligence.quota import MemoryUsageLedger, UsageLedger, next_reset
from app.intelligence.store import AnalysisRecord, AnalysisStore, SchemaNotReady
from app.models.enums import AnalysisStatus, IngestionJobStatus
from app.processing.contract import StoryCluster

logger = logging.getLogger("app.intelligence")
DONE = {AnalysisStatus.COMPLETED, AnalysisStatus.NEEDS_REVIEW, AnalysisStatus.INSUFFICIENT_EVIDENCE}
RETRYABLE = {AnalysisStatus.FAILED, AnalysisStatus.INVALID_OUTPUT}
USABLE = [AnalysisStatus.COMPLETED.value, AnalysisStatus.NEEDS_REVIEW.value]
AlertHook = Callable[[dict[str, Any]], None]


@dataclass
class AnalysisReport:
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "succeeded"  # succeeded | partial | quota_limited | skipped | failed
    reason: str | None = None
    dry_run: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    job_id: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "dryRun": self.dry_run,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "counts": self.counts,
            "usage": self.usage,
            "alerts": self.alerts,
            "jobId": self.job_id,
            "results": self.results,
        }


def needs_work(
    cluster: StoryCluster,
    state: AnalysisRecord | None,
    *,
    key: str,
    now: datetime,
    max_attempts: int,
    retry_failed: bool,
) -> str:
    """'new' | 'changed' | 'retry' | 'resume' to analyse; 'up_to_date' | 'waiting' | 'exhausted' to leave."""
    if state is None:
        return "new"
    if state.cluster_revision != cluster.processing.revision or state.engine_key != key:
        return "changed"
    if state.status in DONE:
        return "up_to_date"
    if state.status == AnalysisStatus.QUOTA_LIMITED:
        return "resume" if not state.next_attempt_at or state.next_attempt_at <= now or retry_failed else "waiting"
    if retry_failed:
        return "retry"
    if state.attempt_count >= max_attempts:
        return "exhausted"
    if state.next_attempt_at and state.next_attempt_at > now:
        return "waiting"
    return "retry"


def check_ready(settings: Settings) -> None:
    """Configuration needed before any call is made. Raises IntelligenceConfigError with a safe message."""
    if settings.gemini_api_key is None:
        raise IntelligenceConfigError(MISSING_KEY)


def log_admin_alert(alert: dict[str, Any]) -> None:
    """The project has no e-mail or chat notifier yet, so alerts go to the error log (structured `admin_alert`
    field for log-based alerting) and onto the run record in ingestion_jobs.details."""
    logger.error("ADMIN ALERT: %s", alert["message"], extra={"admin_alert": alert})


class AnalysisRunner:
    def __init__(
        self,
        settings: Settings,
        store: AnalysisStore,
        clusters: Callable[[], list[StoryCluster]],
        *,
        ledger: UsageLedger | None = None,
        provider_factory: Callable[[Settings], IntelligenceProvider] = build_provider,
        categories: CategorySet | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        alert_hook: AlertHook = log_admin_alert,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.store = store
        self.clusters_source = clusters
        self.ledger = ledger if ledger is not None else MemoryUsageLedger()
        self.provider_factory = provider_factory
        self._categories = categories
        self.clock = clock
        self.sleep = sleep
        self.alert_hook = alert_hook
        self.dry_run = dry_run

    @property
    def categories(self) -> CategorySet:
        if self._categories is None:
            self._categories = load_categories(self.settings.intelligence_categories_file)
        return self._categories

    def run(self, *, retry_failed: bool = False, cluster_ids: list[uuid.UUID] | None = None, limit: int | None = None):
        return asyncio.run(self.run_async(retry_failed=retry_failed, cluster_ids=cluster_ids, limit=limit))

    def _alert(self, report: AnalysisReport, kind: str, message: str, **extra) -> None:
        alert = {"kind": kind, "message": message, "model": self.settings.ai_model, "at": self.clock().isoformat()}
        alert.update(extra)
        report.alerts.append(alert)
        try:
            self.alert_hook(alert)
        except Exception:  # noqa: BLE001 - an alert hook must never break a run
            logger.exception("admin alert hook failed")

    async def run_async(
        self, *, retry_failed: bool = False, cluster_ids: list[uuid.UUID] | None = None, limit: int | None = None
    ) -> AnalysisReport:
        now = self.clock()
        report = AnalysisReport(started_at=now, dry_run=self.dry_run)
        try:
            check_ready(self.settings)
            categories = self.categories
            self.store.check_schema()
        except (IntelligenceConfigError, SchemaNotReady) as exc:
            report.status, report.reason, report.finished_at = "failed", exc.safe_message, self.clock()
            logger.error("analysis run not started: %s", exc.safe_message)
            return report

        snapshot = self.ledger.snapshot(self.settings.ai_model, now)
        if snapshot.quota_exhausted_until is not None:
            report.status = "quota_limited"
            report.reason = (
                f"The Gemini free quota for {self.settings.ai_model} is used up until "
                f"{snapshot.quota_exhausted_until.isoformat()}; no requests sent. Stories stay pending."
            )
            report.usage = snapshot.as_dict(
                requests_per_day=self.settings.ai_requests_per_day, tokens_per_day=self.settings.ai_tokens_per_day
            )
            if not self.dry_run:
                self.store.record_skipped(report.reason, now)
            report.finished_at = self.clock()
            return report

        recovered = self.store.recover_stale(now, self.settings.ingestion_job_stale_minutes)
        if recovered:
            logger.warning("marked %d abandoned job(s) as failed", recovered)
        job_id = self.store.start(now)
        if job_id is None:
            report.status, report.reason, report.finished_at = "skipped", "already running in another process", now
            return report
        report.job_id = str(job_id)
        provider = None
        try:
            provider = self.provider_factory(self.settings)
            await self._run(report, job_id, provider, categories, retry_failed, cluster_ids, limit)
            job_status = IngestionJobStatus.FAILED if report.status == "failed" else IngestionJobStatus.SUCCEEDED
            self.store.finish(
                job_id,
                status=job_status,
                now=self.clock(),
                details={
                    "outcome": report.status,
                    "counts": report.counts,
                    "usage": report.usage,
                    "alerts": report.alerts,
                },
                error=report.reason if report.status in ("failed", "quota_limited") else None,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the scheduler/worker
            report.status = "failed"
            report.reason = getattr(exc, "safe_message", None) or f"Unexpected error ({type(exc).__name__}). See logs."
            logger.exception("analysis run failed")
            try:
                self.store.finish(
                    job_id,
                    status=IngestionJobStatus.FAILED,
                    now=self.clock(),
                    details={"counts": report.counts, "error_type": type(exc).__name__},
                    error=report.reason,
                )
            except Exception:  # noqa: BLE001 - the stale-job sweep closes it later
                logger.exception("could not record analysis run failure")
        finally:
            if provider is not None:
                await provider.aclose()
        report.finished_at = self.clock()
        return report

    async def _run(self, report, job_id, provider, categories, retry_failed, cluster_ids, limit) -> None:
        s = self.settings
        now = report.started_at
        key = engine_key(categories, s)
        clusters = list({c.cluster_id: c for c in self.clusters_source()}.values())  # each cluster once per run
        if cluster_ids is not None:
            wanted = set(cluster_ids)
            clusters = [c for c in clusters if c.cluster_id in wanted]
        states = self.store.load_states([c.cluster_id for c in clusters])
        counts: Counter[str] = Counter(clusters=len(clusters))
        todo: list[tuple[StoryCluster, AnalysisRecord | None]] = []
        for c in clusters:
            state = states.get(c.cluster_id)
            decision = needs_work(
                c, state, key=key, now=now, max_attempts=s.ai_max_attempts_per_story, retry_failed=retry_failed
            )
            counts[decision] += 1
            if decision in ("new", "changed", "retry", "resume"):
                todo.append((c, state))
        cap = min(limit or s.ai_max_stories_per_run, s.ai_max_stories_per_run)
        counts["deferred_by_run_limit"] = max(0, len(todo) - cap)
        todo = todo[:cap]

        analyzer = StoryAnalyzer(s, provider, categories, ledger=self.ledger, sleep=self.sleep, clock=self.clock)
        semaphore = asyncio.Semaphore(s.ai_max_concurrency)
        stop = asyncio.Event()
        totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        stop_error: list = []
        quota_hit: list = []

        async def work(cluster: StoryCluster, state: AnalysisRecord | None) -> None:
            async with semaphore:
                if quota_hit:
                    self._save_quota_limited(cluster, state, key, job_id, counts)
                    return
                if stop.is_set():
                    counts["not_started"] += 1
                    return
                outcome = await analyzer.analyse(cluster, self.clock())
            totals["input_tokens"] += outcome.usage.input_tokens
            totals["output_tokens"] += outcome.usage.output_tokens
            totals["calls"] += outcome.calls
            if outcome.quota_limited:
                quota_hit.append(outcome.error.safe_message)
                self._save_quota_limited(cluster, state, key, job_id, counts)
                return
            if outcome.fatal:
                # Not the story's fault (bad key, model unavailable): keep its previous state and stop the run.
                stop.set()
                stop_error.append(outcome.error)
                counts["not_saved_after_fatal_error"] += 1
                return
            counts[outcome.status.value] += 1
            record = self._record(cluster, state, outcome, key=key, job_id=job_id)
            # A short synchronous write next to multi-second model calls; saved per story so a crash never loses
            # finished work and one story's failure never blocks the others.
            self.store.save(record)
            logger.info(
                "ai usage",
                extra={
                    "ai_usage": {
                        "cluster_id": str(cluster.cluster_id),
                        "status": outcome.status.value,
                        "calls": outcome.calls,
                        "input_tokens": outcome.usage.input_tokens,
                        "output_tokens": outcome.usage.output_tokens,
                    }
                },
            )
            report.results.append(
                {
                    "clusterId": str(cluster.cluster_id),
                    "status": outcome.status.value,
                    "priority": record.priority_level,
                    "category": record.primary_category,
                    "reviewFlags": outcome.review_flags,
                    "calls": outcome.calls,
                    "errors": [e["code"] for e in (record.errors or [])],
                }
            )

        await asyncio.gather(*(work(c, st) for c, st in todo))
        report.counts = {k: v for k, v in counts.items() if v}
        snap = self.ledger.snapshot(s.ai_model, self.clock())
        report.usage = {
            **totals,
            "model": s.ai_model,
            "today": snap.as_dict(requests_per_day=s.ai_requests_per_day, tokens_per_day=s.ai_tokens_per_day),
        }
        if quota_hit:
            report.status, report.reason = "quota_limited", quota_hit[0]
            self._alert(
                report,
                "quota_exhausted",
                quota_hit[0],
                quota_limited_stories=counts["quota_limited"] + counts["deferred_by_quota"],
                resumes_after=next_reset(now).isoformat(),
            )
        elif stop_error:
            err = stop_error[0]
            report.status, report.reason = "failed", err.safe_message
            self._alert(report, f"provider_{err.kind}", err.safe_message)
        elif counts["failed"] or counts["invalid_output"]:
            report.status = "partial"
        logger.info("analysis run %s: %s", report.status, report.counts)

    def _save_quota_limited(self, cluster, state, key, job_id, counts) -> None:
        """Mark a story as waiting for quota. A previous usable analysis is never overwritten."""
        if state is not None and state.analysis is not None:
            counts["deferred_by_quota"] += 1
            return
        counts["quota_limited"] += 1
        now = self.clock()
        same = state is not None and state.cluster_revision == cluster.processing.revision and state.engine_key == key
        self.store.save(
            AnalysisRecord(
                cluster_id=cluster.cluster_id,
                cluster_revision=cluster.processing.revision,
                engine_key=key,
                status=AnalysisStatus.QUOTA_LIMITED.value,
                expires_at=now + timedelta(days=self.settings.intelligence_retention_days),
                attempt_count=state.attempt_count if same else 0,
                last_attempt_at=state.last_attempt_at if state else None,
                next_attempt_at=next_reset(now),
                model_id=self.settings.ai_model,
                errors=[
                    {
                        "code": "quota_limited",
                        "path": "$",
                        "severity": "error",
                        "message": "Waiting for the Gemini free quota to reset.",
                    }
                ],
                job_id=None if self.dry_run else job_id,
            )
        )

    def _record(self, cluster, state, outcome: AnalysisOutcome, *, key, job_id) -> AnalysisRecord:
        s = self.settings
        now = self.clock()
        same = state is not None and state.cluster_revision == cluster.processing.revision and state.engine_key == key
        attempts = (state.attempt_count if same else 0) + 1
        retryable = outcome.status in RETRYABLE
        analysis = outcome.analysis
        return AnalysisRecord(
            cluster_id=cluster.cluster_id,
            cluster_revision=cluster.processing.revision,
            engine_key=key,
            status=outcome.status.value,
            expires_at=now + timedelta(days=s.intelligence_retention_days),
            attempt_count=attempts,
            last_attempt_at=now,
            next_attempt_at=now + timedelta(minutes=s.ai_retry_delay_minutes * attempts) if retryable else None,
            analysed_at=now if analysis else None,
            model_id=outcome.model if outcome.calls else None,
            primary_category=analysis.primary_category_key if analysis else None,
            priority_score=analysis.priority_assessment.overall_score if analysis else None,
            priority_level=analysis.priority_assessment.priority_level if analysis else None,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            analysis=analysis.model_dump(mode="json", by_alias=True) if analysis else None,
            errors=outcome.errors_json(),
            review_flags=outcome.review_flags,
            job_id=None if self.dry_run else job_id,
        )

    def list(
        self, *, statuses: list[str] | None = None, limit: int = 100, updated_since: datetime | None = None
    ) -> list[StoryAnalysis]:
        """Validated analyses (the Stage 3.4 input). Defaults to completed + needs_review."""
        self.store.check_schema()
        rows = self.store.list(now=self.clock(), statuses=statuses or USABLE, limit=limit, updated_since=updated_since)
        return [StoryAnalysis.model_validate(r.analysis) for r in rows if r.analysis]
