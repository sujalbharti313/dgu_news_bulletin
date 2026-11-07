"""Stage 3.3 orchestration: lock, choose which clusters need analysis, analyse them concurrently (bounded),
save each result as it finishes, report. Nothing is ranked or published.

Idempotency: a cluster is analysed again only when its Stage 3.2 revision or the engine key changes, or
when an earlier attempt failed and its retry time has come (at most AI_MAX_ATTEMPTS_PER_STORY attempts,
unless the run is asked to retry failures). Logs carry ids and counts only, never source text.
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
from app.intelligence.contract import StoryIntelligence
from app.intelligence.engine import AnalysisOutcome, StoryAnalyzer, engine_key, estimate_cost
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers import build_provider
from app.intelligence.providers.base import IntelligenceProvider
from app.intelligence.store import IntelligenceRecord, IntelligenceStore, SchemaNotReady
from app.models.enums import IngestionJobStatus, IntelligenceStatus
from app.processing.contract import StoryCluster

logger = logging.getLogger("app.intelligence")
DONE = {IntelligenceStatus.ANALYSED, IntelligenceStatus.NEEDS_REVIEW, IntelligenceStatus.INSUFFICIENT_EVIDENCE}
RETRYABLE = {IntelligenceStatus.FAILED, IntelligenceStatus.INVALID_OUTPUT}
UsageHook = Callable[[dict[str, Any]], None]


@dataclass
class IntelligenceReport:
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "succeeded"  # succeeded | partial | skipped | failed
    reason: str | None = None
    dry_run: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
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
            "jobId": self.job_id,
            "results": self.results,
        }


def needs_work(
    cluster: StoryCluster,
    state: IntelligenceRecord | None,
    *,
    key: str,
    now: datetime,
    max_attempts: int,
    retry_failed: bool,
) -> str:
    """'new' | 'changed' | 'retry' to analyse; 'up_to_date' | 'waiting' | 'exhausted' to leave alone."""
    if state is None:
        return "new"
    if state.cluster_revision != cluster.processing.revision or state.engine_key != key:
        return "changed"
    if state.status in DONE:
        return "up_to_date"
    if retry_failed:
        return "retry"
    if state.attempt_count >= max_attempts:
        return "exhausted"
    if state.next_attempt_at and state.next_attempt_at > now:
        return "waiting"
    return "retry"


def check_ready(settings: Settings) -> None:
    """Configuration needed before any call is made. Raises IntelligenceConfigError with a safe message."""
    if not settings.ai_model:
        raise IntelligenceConfigError("AI_MODEL is not set. Choose a model that supports JSON-schema output.")
    if settings.ai_provider == "openai" and settings.openai_api_key is None:
        raise IntelligenceConfigError("OPENAI_API_KEY is not set (backend/.env or host secrets).")


class IntelligenceRunner:
    def __init__(
        self,
        settings: Settings,
        store: IntelligenceStore,
        clusters: Callable[[], list[StoryCluster]],
        *,
        provider_factory: Callable[[Settings], IntelligenceProvider] = build_provider,
        categories: CategorySet | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        usage_hook: UsageHook | None = None,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.store = store
        self.clusters_source = clusters
        self.provider_factory = provider_factory
        self._categories = categories
        self.clock = clock
        self.sleep = sleep
        self.usage_hook = usage_hook
        self.dry_run = dry_run

    @property
    def categories(self) -> CategorySet:
        if self._categories is None:
            self._categories = load_categories(self.settings.intelligence_categories_file)
        return self._categories

    def run(self, *, retry_failed: bool = False, cluster_ids: list[uuid.UUID] | None = None, limit: int | None = None):
        return asyncio.run(self.run_async(retry_failed=retry_failed, cluster_ids=cluster_ids, limit=limit))

    async def run_async(
        self, *, retry_failed: bool = False, cluster_ids: list[uuid.UUID] | None = None, limit: int | None = None
    ) -> IntelligenceReport:
        now = self.clock()
        report = IntelligenceReport(started_at=now, dry_run=self.dry_run)
        try:
            check_ready(self.settings)
            categories = self.categories
            self.store.check_schema()
        except (IntelligenceConfigError, SchemaNotReady) as exc:
            report.status, report.reason, report.finished_at = "failed", exc.safe_message, self.clock()
            logger.error("intelligence run not started: %s", exc.safe_message)
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
            return report
        report.job_id = str(job_id)
        provider = None
        try:
            provider = self.provider_factory(self.settings)
            await self._run(report, job_id, provider, categories, retry_failed, cluster_ids, limit)
            self.store.finish(
                job_id,
                status=IngestionJobStatus.FAILED if report.status == "failed" else IngestionJobStatus.SUCCEEDED,
                now=self.clock(),
                details={"counts": report.counts, "usage": report.usage},
                error=report.reason if report.status == "failed" else None,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the scheduler/worker
            report.status = "failed"
            report.reason = getattr(exc, "safe_message", None) or f"Unexpected error ({type(exc).__name__}). See logs."
            logger.exception("intelligence run failed")
            try:
                self.store.finish(job_id, status=IngestionJobStatus.FAILED, now=self.clock(),
                                  details={"counts": report.counts, "error_type": type(exc).__name__},
                                  error=report.reason)  # fmt: skip
            except Exception:  # noqa: BLE001 - the stale-job sweep closes it later
                logger.exception("could not record intelligence run failure")
        finally:
            if provider is not None:
                await provider.aclose()
        report.finished_at = self.clock()
        return report

    async def _run(self, report, job_id, provider, categories, retry_failed, cluster_ids, limit) -> None:
        s = self.settings
        now = report.started_at
        key = engine_key(categories, s)
        clusters = self.clusters_source()
        if cluster_ids is not None:
            wanted = set(cluster_ids)
            clusters = [c for c in clusters if c.cluster_id in wanted]
        states = self.store.load_states([c.cluster_id for c in clusters])
        counts: Counter[str] = Counter(clusters=len(clusters))
        todo: list[tuple[StoryCluster, IntelligenceRecord | None]] = []
        for c in clusters:
            state = states.get(c.cluster_id)
            decision = needs_work(
                c, state, key=key, now=now, max_attempts=s.ai_max_attempts_per_story, retry_failed=retry_failed
            )
            counts[decision] += 1
            if decision in ("new", "changed", "retry"):
                todo.append((c, state))
        cap = min(limit or s.ai_max_stories_per_run, s.ai_max_stories_per_run)
        counts["deferred_by_run_limit"] = max(0, len(todo) - cap)
        todo = todo[:cap]

        analyzer = StoryAnalyzer(s, provider, categories, sleep=self.sleep)
        semaphore = asyncio.Semaphore(s.ai_max_concurrency)
        stop = asyncio.Event()
        totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "estimated_cost_usd": 0.0}
        stop_reason: list[str] = []

        async def work(cluster: StoryCluster, state: IntelligenceRecord | None) -> None:
            async with semaphore:
                if stop.is_set():
                    counts["not_started"] += 1
                    return
                if s.ai_max_cost_per_run_usd is not None and totals["estimated_cost_usd"] >= s.ai_max_cost_per_run_usd:
                    counts["deferred_by_cost_limit"] += 1
                    return
                outcome = await analyzer.analyse(cluster, self.clock())
            totals["input_tokens"] += outcome.usage.input_tokens
            totals["output_tokens"] += outcome.usage.output_tokens
            totals["calls"] += outcome.calls
            cost = estimate_cost(s, outcome.usage)
            if cost is not None:
                totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"] + cost, 6)
            counts[outcome.status.value] += 1
            if outcome.fatal:
                # Not the story's fault (bad key, no quota): keep its previous state and stop the run.
                stop.set()
                stop_reason.append(outcome.error.safe_message)
                counts["not_saved_after_fatal_error"] += 1
                return
            record = self._record(cluster, state, outcome, key=key, job_id=job_id, provider=provider.name, cost=cost)
            # A short synchronous write (milliseconds) next to multi-second model calls; saved per story so a
            # crash never loses finished work.
            self.store.save(record)
            self._emit_usage(cluster, outcome, cost)
            report.results.append(
                {"clusterId": str(cluster.cluster_id), "status": outcome.status.value,
                 "reviewFlags": outcome.review_flags, "calls": outcome.calls,
                 "errors": [e["code"] for e in (record.errors or [])]}
            )  # fmt: skip

        await asyncio.gather(*(work(c, st) for c, st in todo))
        report.counts = {k: v for k, v in counts.items() if v}
        report.usage = {**totals, "model": s.ai_model, "cost_estimated": s.ai_input_cost_per_million_tokens is not None}
        if stop_reason:
            report.status, report.reason = "failed", stop_reason[0]
        elif counts["failed"] or counts["invalid_output"]:
            report.status = "partial"
        logger.info("intelligence run %s: %s", report.status, report.counts)

    def _record(self, cluster, state, outcome: AnalysisOutcome, *, key, job_id, provider, cost) -> IntelligenceRecord:
        s = self.settings
        now = self.clock()
        same = state is not None and state.cluster_revision == cluster.processing.revision and state.engine_key == key
        attempts = (state.attempt_count if same else 0) + 1
        retryable = outcome.status in RETRYABLE
        intelligence = outcome.intelligence.model_dump(mode="json", by_alias=True) if outcome.intelligence else None
        return IntelligenceRecord(
            cluster_id=cluster.cluster_id,
            cluster_revision=cluster.processing.revision,
            engine_key=key,
            status=outcome.status.value,
            expires_at=now + timedelta(days=s.intelligence_retention_days),
            attempt_count=attempts,
            last_attempt_at=now,
            next_attempt_at=now + timedelta(minutes=s.ai_retry_delay_minutes * attempts) if retryable else None,
            analysed_at=now if intelligence else None,
            provider=provider if outcome.calls or outcome.error else None,
            model=outcome.model,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            estimated_cost_usd=cost,
            intelligence=intelligence,
            errors=outcome.errors_json(),
            review_flags=outcome.review_flags,
            job_id=None if self.dry_run else job_id,
        )

    def _emit_usage(self, cluster: StoryCluster, outcome: AnalysisOutcome, cost: float | None) -> None:
        event = {
            "cluster_id": str(cluster.cluster_id),
            "status": outcome.status.value,
            "model": outcome.model,
            "calls": outcome.calls,
            "input_tokens": outcome.usage.input_tokens,
            "output_tokens": outcome.usage.output_tokens,
            "estimated_cost_usd": cost,
        }
        logger.info("ai usage", extra={"ai_usage": event})
        if self.usage_hook is not None:
            try:
                self.usage_hook(event)
            except Exception:  # noqa: BLE001 - a monitoring hook must never break a run
                logger.exception("usage hook failed")

    def list(
        self, *, statuses: list[str] | None = None, limit: int = 100, updated_since: datetime | None = None
    ) -> list[StoryIntelligence]:
        """Validated intelligence (the Stage 3.4 input). Defaults to analysed + needs_review."""
        self.store.check_schema()
        statuses = statuses or [IntelligenceStatus.ANALYSED.value, IntelligenceStatus.NEEDS_REVIEW.value]
        rows = self.store.list(now=self.clock(), statuses=statuses, limit=limit, updated_since=updated_since)
        return [StoryIntelligence.model_validate(r.intelligence) for r in rows if r.intelligence]
