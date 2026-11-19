"""Stage 3.4 orchestration for one edition week:

lock → load clusters and Stage 3.3 analyses → validate candidates → build each story's semantic representation →
reuse or generate Gemini embeddings (content-hash cache, batched, quota-aware) → store them in pgvector →
retrieve related stories by cosine similarity in the database → Gemini editorial evaluation with the related
stories as context (cached per input, quota-aware) → deterministic verification, caps, weighted scores and
quality gates → same-event grouping and earlier-edition check → rank → diversity-aware shortlist → explain → save.

Reliability:
- One run at a time across processes (ingestion_jobs lock). Re-running on unchanged inputs reuses every
  embedding and evaluation, produces the same `run_key` and updates the same `ranking_runs` row: no duplicate
  embeddings, no duplicate runs, no quota used.
- Failure isolation: one story that cannot be embedded, retrieved, evaluated or saved is reported and the others
  continue. Technical failures are never editorial rejections: those stories are `pending`, persisted with
  their retry state (attempts, next attempt time) and retried on a later run; `--retry-failed` retries now.
- Quota: when the free quota of the embedding or the evaluation model is used up, no further requests are sent,
  the waiting stories stay pending, an admin alert is raised and the run is `incomplete`. No fallback model, no
  other provider, nothing paid.
- Nothing is published, approved or deleted here.
"""

import asyncio
import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.intelligence.contract import StoryAnalysis
from app.intelligence.evidence import build_evidence
from app.intelligence.providers import build_provider
from app.intelligence.providers.gemini import MISSING_KEY
from app.intelligence.quota import MemoryUsageLedger, UsageLedger, next_reset
from app.intelligence.runner import log_admin_alert
from app.intelligence.store import AnalysisRecord
from app.models.enums import IngestionJobStatus
from app.processing.contract import StoryCluster
from app.ranking.config import (
    RankingConfig,
    RankingConfigError,
    SourceRegistry,
    load_ranking_config,
    load_source_registry,
    verification_version,
)
from app.ranking.contract import (
    ExcludedStory,
    FailedEvaluation,
    PendingStory,
    RankedCandidate,
    RankingResult,
    RankingTotals,
    SemanticRelation,
)
from app.ranking.duplicates import apply_duplicate_gate, apply_previously_shortlisted, extra_publishers
from app.ranking.embeddings import EMBEDDING_INPUT_VERSION, Embedder, EmbeddingJob, content_hash, embedding_text
from app.ranking.evaluate import Evaluated, evaluate_story
from app.ranking.semantic import EVALUATION_VERSION, EvaluationDraft, Evaluator, RelatedStory, evaluation_key
from app.ranking.shortlist import explain, rank, select_shortlist
from app.ranking.store import EmbeddingRecord, RankingStore, RunRecord, SchemaNotReady, VerificationRecord
from app.ranking.window import EditionWindow, edition_for

logger = logging.getLogger("app.ranking")
AlertHook = Callable[[dict[str, Any]], None]
TECHNICAL_STATUS = {
    "embedding_pending": "pending_embedding",
    "embedding_failed": "embedding_failed",
    "evaluation_pending": "pending_evaluation",
    "evaluation_failed": "evaluation_failed",
}


@dataclass
class RankingReport:
    started_at: datetime
    status: str = "completed"  # completed | incomplete | failed | skipped
    reason: str | None = None
    finished_at: datetime | None = None
    dry_run: bool = False
    result: RankingResult | None = None
    run_id: str | None = None
    reused_run: bool = False
    job_id: str | None = None
    ai_usage: dict[str, Any] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "dryRun": self.dry_run,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "runId": self.run_id,
            "reusedRun": self.reused_run,
            "jobId": self.job_id,
            "aiUsage": self.ai_usage,
            "alerts": self.alerts,
            "result": self.result.model_dump(mode="json", by_alias=True) if self.result else None,
        }


@dataclass
class StoryState:
    """Everything the run knows about one candidate story, including its retry state."""

    cluster: StoryCluster
    analysis: StoryAnalysis
    record: AnalysisRecord
    prev: VerificationRecord | None
    embed_text: str
    embed_hash: str
    embedding_status: str = "pending"  # completed | cached | pending | quota_limited | failed | invalid_output
    embedding_attempts: int = 0
    embedding_next_at: datetime | None = None
    similar: list = field(default_factory=list)
    related: list[RelatedStory] = field(default_factory=list)
    eval_key: str | None = None
    draft: EvaluationDraft | None = None
    eval_status: str = "pending"  # completed | cached | pending | quota_limited | failed | invalid_output
    eval_attempts: int = 0
    eval_next_at: datetime | None = None
    eval_model_version: str | None = None
    errors: list[dict] | None = None

    @property
    def cluster_id(self) -> uuid.UUID:
        return self.cluster.cluster_id

    @property
    def embedded(self) -> bool:
        return self.embedding_status in ("completed", "cached")

    @property
    def evaluated(self) -> bool:
        return self.draft is not None and self.eval_status in ("completed", "cached")

    def hold_reason(self) -> tuple[str, str] | None:
        """(pending reason, detail) when the story cannot be ranked yet for a technical reason."""
        if not self.embedded:
            if self.embedding_status in ("failed", "invalid_output"):
                return "embedding_failed", f"Embedding {self.embedding_status.replace('_', ' ')}; retried later."
            return "embedding_pending", "Embedding waits for the Gemini free quota or the next run."
        if not self.evaluated:
            if self.eval_status in ("failed", "invalid_output"):
                return "evaluation_failed", f"Editorial evaluation {self.eval_status.replace('_', ' ')}; retried later."
            return "evaluation_pending", "Editorial evaluation waits for the Gemini free quota or the next run."
        return None


def _hash(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def in_window(cluster: StoryCluster, window: EditionWindow) -> bool:
    """Collected during the edition week (Monday-Sunday, Asia/Kolkata)."""
    return cluster.first_collected_at < window.end and cluster.last_collected_at >= window.start


class RankingRunner:
    def __init__(
        self,
        settings: Settings,
        store: RankingStore,
        clusters: Callable[[], list[StoryCluster]],
        analyses: Callable[[], list[AnalysisRecord]],
        *,
        ledger: UsageLedger | None = None,
        provider_factory: Callable[[Settings], Any] = build_provider,
        config: RankingConfig | None = None,
        registry: SourceRegistry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        alert_hook: AlertHook = log_admin_alert,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.store = store
        self.clusters_source = clusters
        self.analyses_source = analyses
        self.ledger = ledger if ledger is not None else MemoryUsageLedger()
        self.provider_factory = provider_factory
        self._config = config
        self._registry = registry
        self.clock = clock
        self.sleep = sleep
        self.alert_hook = alert_hook
        self.dry_run = dry_run
        self._provider = None

    @property
    def config(self) -> RankingConfig:
        if self._config is None:
            self._config = load_ranking_config(self.settings.ranking_config_file)
        return self._config

    @property
    def registry(self) -> SourceRegistry:
        if self._registry is None:
            self._registry = load_source_registry(self.settings.source_registry_file)
        return self._registry

    def versions(self) -> tuple[str, str]:
        key = f"eval-{EVALUATION_VERSION}+{EMBEDDING_INPUT_VERSION}+{self.settings.embedding_model}"
        return self.config.ranking_version, verification_version(self.config, self.registry, key)[:128]

    def run(self, *, week: str = "previous", retry_failed: bool = False) -> RankingReport:
        return asyncio.run(self.run_async(week=week, retry_failed=retry_failed))

    def _alert(self, report: RankingReport, kind: str, message: str, **extra) -> None:
        alert = {"kind": kind, "message": message, "stage": "3.4", "at": self.clock().isoformat(), **extra}
        report.alerts.append(alert)
        try:
            self.alert_hook(alert)
        except Exception:  # noqa: BLE001 - an alert hook must never break a run
            logger.exception("admin alert hook failed")

    def _provider_instance(self):
        if self._provider is None:
            self._provider = self.provider_factory(self.settings)
        return self._provider

    async def run_async(self, *, week: str = "previous", retry_failed: bool = False) -> RankingReport:
        now = self.clock()
        report = RankingReport(started_at=now, dry_run=self.dry_run)
        try:
            config, registry = self.config, self.registry
            window = edition_for(now, week=week, timezone_name=config.timeliness.timezone)
            if self.settings.gemini_api_key is None:
                raise RankingConfigError(
                    f"{MISSING_KEY} Stage 3.4 needs it for embeddings and the editorial evaluation (free tier)."
                )
            self.store.check_schema(self.settings.embedding_dimensions)
        except (RankingConfigError, SchemaNotReady, ValueError) as exc:
            report.status = "failed"
            report.reason = getattr(exc, "safe_message", None) or str(exc)
            report.finished_at = self.clock()
            logger.error("ranking run not started: %s", report.reason)
            return report

        recovered = self.store.recover_stale(now, self.settings.ingestion_job_stale_minutes)
        if recovered:
            logger.warning("marked %d abandoned job(s) as failed", recovered)
        job_id = self.store.start(now)
        if job_id is None:
            report.status, report.reason, report.finished_at = "skipped", "already running in another process", now
            return report
        report.job_id = str(job_id)
        try:
            await self._run(report, job_id, window, config, registry, retry_failed)
            self.store.finish(
                job_id,
                status=IngestionJobStatus.SUCCEEDED,
                now=self.clock(),
                details={
                    "outcome": report.status,
                    "edition": window.start_date.isoformat(),
                    "runId": report.run_id,
                    "totals": report.result.totals.model_dump(mode="json") if report.result else None,
                    "aiUsage": report.ai_usage,
                    "alerts": report.alerts,
                },
                error=report.reason if report.status == "incomplete" else None,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the scheduler
            report.status = "failed"
            report.reason = getattr(exc, "safe_message", None) or f"Unexpected error ({type(exc).__name__}). See logs."
            logger.exception("ranking run failed")
            try:
                self.store.finish(
                    job_id,
                    status=IngestionJobStatus.FAILED,
                    now=self.clock(),
                    details={"error_type": type(exc).__name__},
                    error=report.reason,
                )
            except Exception:  # noqa: BLE001 - the stale-job sweep closes it later
                logger.exception("could not record ranking run failure")
        finally:
            if self._provider is not None:
                try:
                    await self._provider.aclose()
                except Exception:  # noqa: BLE001, S110 - closing is best effort
                    pass
                self._provider = None
        report.finished_at = self.clock()
        return report

    # ------------------------------------------------------------------------------------------------------
    def _candidates(self, clusters, records, stored):
        s = self.settings
        pending: list[PendingStory] = []
        excluded: list[ExcludedStory] = []
        failed: list[FailedEvaluation] = []
        states: list[StoryState] = []
        for c in clusters:
            rec = records.get(c.cluster_id)
            if rec is None:
                pending.append(
                    PendingStory(
                        story_cluster_id=c.cluster_id,
                        reason="not_analysed",
                        detail="Stage 3.3 has not analysed this story yet.",
                    )
                )
            elif rec.status == "quota_limited":
                pending.append(
                    PendingStory(
                        story_cluster_id=c.cluster_id,
                        reason="analysis_quota_limited",
                        detail="Stage 3.3 is waiting for the Gemini free quota.",
                    )
                )
            elif rec.status in ("failed", "invalid_output"):
                if rec.attempt_count >= s.ai_max_attempts_per_story:
                    failed.append(
                        FailedEvaluation(
                            story_cluster_id=c.cluster_id,
                            error=f"Stage 3.3 analysis {rec.status} after {rec.attempt_count} attempts",
                        )
                    )
                else:
                    pending.append(
                        PendingStory(
                            story_cluster_id=c.cluster_id,
                            reason="analysis_failed",
                            detail=f"Stage 3.3 analysis {rec.status}; it will be retried.",
                        )
                    )
            elif rec.analysis is None:
                excluded.append(
                    ExcludedStory(
                        story_cluster_id=c.cluster_id,
                        reason="insufficient_source_text",
                        detail="Too little source text for Stage 3.3 to analyse; not rankable.",
                    )
                )
            elif rec.cluster_revision != c.processing.revision:
                pending.append(
                    PendingStory(
                        story_cluster_id=c.cluster_id,
                        reason="analysis_out_of_date",
                        detail="New sources arrived since the Stage 3.3 analysis; re-run the intelligence engine.",
                    )
                )
            else:
                try:
                    analysis = StoryAnalysis.model_validate(rec.analysis)
                    text_ = embedding_text(c, analysis, max_chars=s.embedding_max_input_chars)
                except Exception as exc:  # noqa: BLE001 - one unreadable analysis must not stop the run
                    failed.append(
                        FailedEvaluation(
                            story_cluster_id=c.cluster_id, error=f"stored analysis is invalid ({type(exc).__name__})"
                        )
                    )
                    continue
                prev = stored.get(c.cluster_id)
                states.append(
                    StoryState(
                        cluster=c,
                        analysis=analysis,
                        record=rec,
                        prev=prev,
                        embed_text=text_,
                        embed_hash=content_hash(text_, model=s.embedding_model, dimensions=s.embedding_dimensions),
                        embedding_attempts=prev.embedding_attempts if prev else 0,
                        embedding_next_at=prev.embedding_next_attempt_at if prev else None,
                    )
                )
        return states, pending, excluded, failed

    async def _embed(self, report, states: list[StoryState], retry_failed: bool, now: datetime) -> None:
        s = self.settings
        existing = self.store.load_embeddings([st.cluster_id for st in states])
        todo: list[StoryState] = []
        for st in states:
            e = existing.get(st.cluster_id)
            if (
                e is not None
                and e.content_hash == st.embed_hash
                and e.model == s.embedding_model
                and e.embedding_version == EMBEDDING_INPUT_VERSION
                and e.dimensions == s.embedding_dimensions
            ):
                st.embedding_status, st.embedding_attempts, st.embedding_next_at = "cached", 0, None
                continue
            prev_status = st.prev.embedding_status if st.prev else "pending"
            if not retry_failed and prev_status in ("failed", "invalid_output"):
                if st.embedding_attempts >= s.ranking_max_attempts or (
                    st.embedding_next_at and st.embedding_next_at > now
                ):
                    st.embedding_status = prev_status
                    continue
            todo.append(st)
        usage = {"requested": len(todo), "embedded": 0, "calls": 0, "requests": 0}
        report.ai_usage["embeddings"] = usage
        if not todo:
            return
        snapshot = self.ledger.snapshot(s.embedding_model, now)
        if snapshot.quota_exhausted_until is not None and snapshot.quota_exhausted_until > now:
            for st in todo:
                st.embedding_status = "quota_limited"
            self._alert(
                report,
                "quota_exhausted",
                f"The Gemini free quota for {s.embedding_model} is used up until "
                f"{snapshot.quota_exhausted_until.isoformat()}; {len(todo)} stories wait for an embedding.",
            )
            return
        embedder = Embedder(s, self._provider_instance(), ledger=self.ledger, sleep=self.sleep, clock=self.clock)
        result = await embedder.embed([EmbeddingJob(st.cluster_id, st.embed_text, st.embed_hash) for st in todo])
        usage["calls"], usage["requests"] = result.calls, result.requests
        for st in todo:
            outcome = result.outcomes[st.cluster_id]
            if outcome.status == "completed":
                record = EmbeddingRecord(
                    cluster_id=st.cluster_id,
                    cluster_revision=st.cluster.processing.revision,
                    embedding=outcome.vector,
                    model=s.embedding_model,
                    dimensions=s.embedding_dimensions,
                    embedding_version=EMBEDDING_INPUT_VERSION,
                    content_hash=st.embed_hash,
                    input_chars=len(st.embed_text),
                    first_collected_at=st.cluster.first_collected_at,
                    last_collected_at=st.cluster.last_collected_at,
                    embedded_at=self.clock(),
                )
                try:
                    self.store.save_embedding(record)
                except Exception as exc:  # noqa: BLE001 - isolated: the story waits for the next run
                    logger.exception("could not save embedding for %s", st.cluster_id)
                    st.embedding_status = "failed"
                    st.errors = [{"code": "embedding_save_failed", "message": type(exc).__name__}]
                    continue
                st.embedding_status, st.embedding_attempts, st.embedding_next_at = "completed", 0, None
                usage["embedded"] += 1
            elif outcome.status == "quota_limited" or outcome.error_code == "stopped":
                st.embedding_status = "quota_limited" if outcome.status == "quota_limited" else "failed"
                st.errors = [{"code": outcome.error_code or "quota", "message": (outcome.error or "")[:200]}]
            else:
                st.embedding_attempts += 1
                st.embedding_status = outcome.status
                st.embedding_next_at = self.clock() + timedelta(
                    minutes=s.ai_retry_delay_minutes * st.embedding_attempts
                )
                st.errors = [{"code": outcome.error_code or "failed", "message": (outcome.error or "")[:200]}]
        if result.stopped:
            kind = "quota_exhausted" if result.quota_stopped else "provider_error"
            waiting = sum(1 for st in states if not st.embedded)
            self._alert(
                report, kind, result.stopped, waiting_stories=waiting, resumes_after=next_reset(now).isoformat()
            )

    def _retrieve(self, states: list[StoryState], records, window: EditionWindow, config, failed) -> None:
        sem = config.semantic
        start = window.start - timedelta(days=sem.lookback_days)
        for st in states:
            if not st.embedded:
                continue
            try:
                found = self.store.similar(
                    st.cluster_id,
                    model=self.settings.embedding_model,
                    version=EMBEDDING_INPUT_VERSION,
                    window_start=start,
                    window_end=window.end,
                    min_similarity=sem.related_threshold,
                    limit=sem.retrieval_limit,
                )
            except Exception as exc:  # noqa: BLE001 - retrieval failure: evaluated without context next time
                logger.exception("similarity search failed for %s", st.cluster_id)
                st.embedding_status = "failed"
                st.errors = [{"code": "retrieval_failed", "message": type(exc).__name__}]
                continue
            related: list[RelatedStory] = []
            kept = []
            for hit in found:
                rec = records.get(hit.cluster_id)
                if hit.cluster_id == st.cluster_id or rec is None or rec.analysis is None:
                    continue
                try:
                    other = StoryAnalysis.model_validate(rec.analysis)
                except Exception:  # noqa: BLE001, S112 - an unreadable related analysis is skipped
                    continue
                date = next((s.published_at for s in other.sources if s.published_at), None)
                related.append(
                    RelatedStory(
                        ref_id=f"R{len(related) + 1}",
                        cluster_id=hit.cluster_id,
                        similarity=hit.similarity,
                        in_window=hit.first_collected_at < window.end and hit.last_collected_at >= window.start,
                        headline=other.headline,
                        main_event=other.understanding.main_event,
                        summary=other.summary,
                        published=date.date().isoformat() if date else None,
                    )
                )
                kept.append(hit)
            st.similar, st.related = kept, related

    async def _evaluate(self, report, states: list[StoryState], retry_failed: bool, now: datetime) -> None:
        s = self.settings
        wanted: list[StoryState] = []
        for st in states:
            if not st.embedded:
                continue
            st.eval_key = evaluation_key(st.analysis, st.related, s)
            prev = st.prev
            if prev is not None and prev.evaluation_key == st.eval_key:
                st.eval_attempts, st.eval_next_at = prev.evaluation_attempts, prev.evaluation_next_attempt_at
                if prev.evaluation and prev.evaluation_status in ("completed", "cached"):
                    try:
                        st.draft = EvaluationDraft.model_validate(prev.evaluation)
                        st.eval_status = "cached"
                        st.eval_model_version = (prev.verification or {}).get("assessment", {}).get("modelVersion")
                        continue
                    except Exception:  # noqa: BLE001 - a bad cache entry is simply evaluated again
                        st.draft = None
                if not retry_failed and prev.evaluation_status in ("failed", "invalid_output"):
                    if st.eval_attempts >= s.ranking_max_attempts or (st.eval_next_at and st.eval_next_at > now):
                        st.eval_status = prev.evaluation_status
                        continue
            else:
                st.eval_attempts, st.eval_next_at = 0, None
            wanted.append(st)
        # Most promising first, so a limited quota is spent where it matters (Stage 3.3 preliminary priority).
        wanted.sort(key=lambda x: (-(x.record.priority_score or 0), str(x.cluster_id)))
        usage = {"requested": len(wanted), "evaluated": 0, "calls": 0, "inputTokens": 0, "outputTokens": 0}
        report.ai_usage["evaluations"] = usage
        if not wanted:
            return
        snapshot = self.ledger.snapshot(s.ai_model, now)
        if snapshot.quota_exhausted_until is not None and snapshot.quota_exhausted_until > now:
            for st in wanted:
                st.eval_status = "quota_limited"
            self._alert(
                report,
                "quota_exhausted",
                f"The Gemini free quota for {s.ai_model} is used up until "
                f"{snapshot.quota_exhausted_until.isoformat()}; {len(wanted)} stories wait for an evaluation.",
            )
            return
        budget = s.ranking_max_evaluations_per_run
        for st in wanted[budget:]:
            st.eval_status = "pending"
        todo = wanted[:budget]
        if not todo:
            return
        evaluator = Evaluator(s, self._provider_instance(), ledger=self.ledger, sleep=self.sleep, clock=self.clock)
        semaphore = asyncio.Semaphore(s.ai_max_concurrency)
        stop: list[str] = []

        async def one(st: StoryState) -> None:
            async with semaphore:
                if stop:
                    st.eval_status = "pending"
                    return
                outcome = await evaluator.evaluate(st.analysis, build_evidence(st.cluster), st.related)
            usage["calls"] += outcome.calls
            usage["inputTokens"] += outcome.usage.input_tokens
            usage["outputTokens"] += outcome.usage.output_tokens
            if outcome.status == "completed":
                st.draft, st.eval_status, st.eval_attempts, st.eval_next_at = outcome.draft, "completed", 0, None
                st.eval_model_version = outcome.model_version
                usage["evaluated"] += 1
                return
            if outcome.status == "quota_limited" or outcome.fatal:
                stop.append(outcome.error.safe_message if outcome.error else "stopped")
                st.eval_status = "quota_limited" if outcome.status == "quota_limited" else "failed"
                st.errors = [{"code": f"provider_{outcome.error.kind}", "message": outcome.error.safe_message}]
                return
            st.eval_attempts += 1
            st.eval_status = outcome.status
            st.eval_next_at = self.clock() + timedelta(minutes=s.ai_retry_delay_minutes * st.eval_attempts)
            st.errors = (
                [{"code": f"provider_{outcome.error.kind}", "message": outcome.error.safe_message}]
                if outcome.error
                else [{"code": "invalid_output", "message": p[:200]} for p in outcome.problems[:5]]
            )

        await asyncio.gather(*(one(st) for st in todo))
        if stop:
            kind = "quota_exhausted" if evaluator.quota_stopped else "provider_error"
            waiting = sum(1 for st in states if st.embedded and not st.evaluated)
            self._alert(report, kind, stop[0], waiting_stories=waiting, resumes_after=next_reset(now).isoformat())

    @staticmethod
    def _relations(st: StoryState) -> list[SemanticRelation]:
        by_ref = {r.ref_id: r for r in st.related}
        out = []
        for v in st.draft.related if st.draft else []:
            r = by_ref.get(v.related_id)
            if r is None:
                continue
            out.append(
                SemanticRelation(
                    story_cluster_id=r.cluster_id,
                    similarity=round(max(-1.0, min(1.0, r.similarity)), 4),
                    in_window=r.in_window,
                    relation=v.relation,
                    conflicts_with_story=v.conflicts_with_story,
                    explanation=v.explanation,
                    headline=r.headline,
                )
            )
        return out

    # ------------------------------------------------------------------------------------------------------
    async def _run(self, report, job_id, window: EditionWindow, config, registry, retry_failed):
        s = self.settings
        now = report.started_at
        ranking_v, verification_v = self.versions()
        all_clusters = {c.cluster_id: c for c in self.clusters_source()}
        clusters = [c for c in all_clusters.values() if in_window(c, window)]
        records = {r.cluster_id: r for r in self.analyses_source()}
        stored = self.store.load_verifications([c.cluster_id for c in clusters])
        states, pending, excluded, failed = self._candidates(clusters, records, stored)

        await self._embed(report, states, retry_failed, now)
        self._retrieve(states, records, window, config, failed)
        await self._evaluate(report, states, retry_failed, now)

        ready = [st for st in states if st.hold_reason() is None]
        held = [st for st in states if st.hold_reason() is not None]
        by_state = {st.cluster_id: st for st in states}

        def evaluate(st: StoryState, extra: set[str] | None = None) -> Evaluated | None:
            try:
                return evaluate_story(
                    st.cluster,
                    st.analysis,
                    window,
                    config,
                    registry,
                    st.draft,
                    relations=self._relations(st),
                    extra_publishers=extra,
                    evaluation_status=st.eval_status,
                    model_id=s.ai_model,
                    model_version=st.eval_model_version,
                )
            except Exception as exc:  # noqa: BLE001 - failure isolation per story
                logger.exception("evaluation failed for cluster %s", st.cluster_id)
                failed.append(
                    FailedEvaluation(story_cluster_id=st.cluster_id, error=f"evaluation error ({type(exc).__name__})")
                )
                return None

        stories = [e for e in (evaluate(st) for st in ready) if e is not None]
        extras = extra_publishers(stories, config)
        if extras:
            stories = [
                (evaluate(by_state[e.cluster_id], extras[e.cluster_id]) or e) if e.cluster_id in extras else e
                for e in stories
            ]
        apply_duplicate_gate(stories, config)
        apply_previously_shortlisted(stories, self.store.shortlisted_before(window.start_date), config)
        stories = rank(stories)

        shortlist, notes = select_shortlist(stories, config)
        positions = {e.cluster_id: i + 1 for i, e in enumerate(shortlist)}
        candidates = [self._candidate(e, i, positions.get(e.cluster_id)) for i, e in enumerate(stories, start=1)]
        for st in held:
            reason, detail = st.hold_reason()
            pending.append(PendingStory(story_cluster_id=st.cluster_id, reason=reason, detail=detail))
        counts = Counter(c.eligibility_status for c in candidates)
        status = "incomplete" if pending else "completed"
        reason = None
        if pending:
            why = Counter(p.reason for p in pending)
            reason = "Not a final ranking: " + ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in why.items())
        if not clusters:
            notes.append("no stories were collected in this edition week")
        by_id = {c.story_cluster_id: c for c in candidates}
        ai_info = {
            "embeddingModel": s.embedding_model,
            "embeddingDimensions": s.embedding_dimensions,
            "embeddingVersion": EMBEDDING_INPUT_VERSION,
            "evaluationModel": s.ai_model,
            "evaluationPromptVersion": EVALUATION_VERSION,
            "embeddingStatuses": dict(Counter(st.embedding_status for st in states)),
            "evaluationStatuses": dict(Counter(st.eval_status for st in states if st.embedded)),
            **report.ai_usage,
        }
        result_base = dict(
            edition=window.as_dict(),
            processed_at=now,
            status=status,
            status_reason=reason,
            totals=RankingTotals(
                clusters_in_window=len(clusters),
                evaluated=len(candidates) + len(excluded),
                eligible=counts["eligible"],
                review_required=counts["review_required"],
                ineligible=counts["ineligible"] + len(excluded),
                pending=len(pending),
                failed=len(failed),
                shortlisted=len(shortlist),
            ),
            ranked_candidate_ids=[e.cluster_id for e in shortlist],
            shortlist=[by_id[e.cluster_id] for e in shortlist],
            candidates=candidates,
            pending=pending,
            excluded=excluded,
            failed=failed,
            ranking_config_version=ranking_v,
            verification_config_version=verification_v,
            ai=ai_info,
            notes=notes,
            dry_run=self.dry_run,
        )
        input_keys = {
            st.cluster_id: _hash(
                st.cluster.processing.revision,
                st.analysis.generation.generated_at.isoformat(),
                st.embed_hash,
                st.eval_key,
                "ready" if st.hold_reason() is None else st.hold_reason()[0],
                ranking_v,
                verification_v,
                window.start_date,
            )[:64]
            for st in states
        }
        run_key = _hash(
            window.start_date,
            ranking_v,
            verification_v,
            sorted(f"{k}:{v}" for k, v in input_keys.items()),
            sorted(f"{p.story_cluster_id}:{p.reason}" for p in pending),
            sorted(str(x.story_cluster_id) for x in excluded),
            sorted(str(f.story_cluster_id) for f in failed),
        )[:64]
        existing = self.store.find_run(run_key)
        report.reused_run = existing is not None
        run = RunRecord(
            run_key=run_key,
            edition_start=window.start_date,
            edition_end=window.end_date,
            timezone=window.timezone_name,
            status=status,
            status_reason=reason,
            ranking_config_version=ranking_v,
            verification_config_version=verification_v,
            processed_at=now,
            result={},
            total_evaluated=len(candidates) + len(excluded),
            total_eligible=counts["eligible"],
            total_review_required=counts["review_required"],
            total_ineligible=counts["ineligible"] + len(excluded),
            total_pending=len(pending),
            total_failed=len(failed),
            shortlist_cluster_ids=[e.cluster_id for e in shortlist],
            errors=[f.model_dump(mode="json") for f in failed] or None,
            job_id=None if self.dry_run else job_id,
        )
        run_id = self.store.save_run(run)  # first, so story rows can point at it
        failed_ids = {f.story_cluster_id for f in failed}
        rows: list[VerificationRecord] = []
        for e, cand in zip(stories, candidates, strict=True):
            rows.append(
                self._record(
                    e, cand, by_state[e.cluster_id], input_keys[e.cluster_id], ranking_v, verification_v, window, run_id
                )
            )
        for st in held:
            if st.cluster_id not in failed_ids:
                rows.append(self._held_record(st, input_keys[st.cluster_id], ranking_v, verification_v, window, run_id))
        for x in excluded:
            rows.append(self._excluded_record(x, clusters, ranking_v, verification_v, window, run_id))
        for row in rows:
            try:
                self.store.save_verification(row)
            except Exception as exc:  # noqa: BLE001 - a failed write is isolated and reported
                logger.exception("could not save verification for %s", row.cluster_id)
                failed.append(
                    FailedEvaluation(story_cluster_id=row.cluster_id, error=f"save failed ({type(exc).__name__})")
                )
        result_base["failed"] = failed
        result_base["totals"] = result_base["totals"].model_copy(update={"failed": len(failed)})
        result = RankingResult(run_id=run_id, **result_base)
        run.result = result.model_dump(mode="json", by_alias=True)
        run.total_failed = len(failed)
        run.errors = [f.model_dump(mode="json") for f in failed] or None
        self.store.save_run(run)
        report.result = result
        report.run_id = str(run_id)
        report.status = status
        report.reason = reason
        logger.info(
            "ranking run %s for %s: %d evaluated, %d eligible, %d shortlisted, %d pending",
            status,
            window.start_date,
            result.totals.evaluated,
            result.totals.eligible,
            result.totals.shortlisted,
            result.totals.pending,
        )

    def _candidate(self, e: Evaluated, position: int, shortlist_rank: int | None) -> RankedCandidate:
        return RankedCandidate(
            story_cluster_id=e.cluster_id,
            headline=e.analysis.headline,
            primary_category=e.analysis.primary_category,
            primary_category_key=e.analysis.primary_category_key,
            organisation=e.organisation,
            coverage_groups=e.coverage_groups,
            development_type=e.development_type,
            final_scores=e.scores,
            priority_classification=e.priority,
            verification_status=e.verification.verification_status,
            eligibility_status=e.eligibility,
            review_flags=e.review_flags(),
            gates=e.gates,
            ranking_explanation=explain(e, shortlist_rank=shortlist_rank),
            rank=position,
            shortlisted=shortlist_rank is not None,
            shortlist_rank=shortlist_rank,
            selection_note=e.selection_note,
            duplicate_of=e.duplicate_of,
            timing=e.timely.timing,
            cluster_revision=e.cluster.processing.revision,
            analysis_generated_at=e.analysis.generation.generated_at,
            verification=e.verification,
        )

    def _base(self, st: StoryState, input_key, ranking_v, verification_v, window, run_id) -> dict:
        now = self.clock()
        return dict(
            cluster_id=st.cluster_id,
            cluster_revision=st.cluster.processing.revision,
            input_key=input_key,
            ranking_config_version=ranking_v,
            verification_config_version=verification_v,
            edition_start=window.start_date,
            evaluated_at=now,
            expires_at=now + timedelta(days=self.settings.ranking_retention_days),
            analysis_generated_at=st.analysis.generation.generated_at,
            source_count=st.cluster.source_count,
            related=[
                {
                    "clusterId": str(r.cluster_id),
                    "similarity": round(r.similarity, 4),
                    "inWindow": r.in_window,
                }
                for r in st.related
            ]
            or None,
            embedding_status=st.embedding_status if st.embedding_status != "cached" else "completed",
            embedding_attempts=st.embedding_attempts,
            embedding_next_attempt_at=st.embedding_next_at,
            evaluation_status=st.eval_status,
            evaluation_key=st.eval_key,
            evaluation=st.draft.model_dump(mode="json") if st.draft is not None else None,
            evaluation_model=self.settings.ai_model if st.draft is not None else None,
            evaluation_attempts=st.eval_attempts,
            evaluation_next_attempt_at=st.eval_next_at,
            errors=st.errors,
            ranking_run_id=None if self.dry_run else run_id,
        )

    def _record(
        self, e: Evaluated, cand: RankedCandidate, st: StoryState, input_key, ranking_v, verification_v, window, run_id
    ):
        v = e.verification
        scores = cand.final_scores
        return VerificationRecord(
            **self._base(st, input_key, ranking_v, verification_v, window, run_id),
            verification_status=v.verification_status,
            eligibility=e.eligibility,
            source_credibility_score=v.source_credibility_score,
            source_credibility_level=v.source_credibility_level,
            evidence_quality_score=v.evidence_quality_score,
            claim_consistency_score=v.claim_consistency_score,
            technical_significance_score=scores.technical_significance,
            global_impact_score=scores.global_impact,
            student_relevance_score=scores.student_relevance,
            timeliness_score=scores.timeliness,
            corroboration_level=v.corroboration_level,
            independent_source_count=v.independent_source_count,
            final_priority_score=scores.final_priority_score,
            priority_classification=cand.priority_classification,
            dimension_scores=scores.model_dump(mode="json", by_alias=True),
            verification=v.model_dump(mode="json", by_alias=True),
            review_flags=cand.review_flags,
            ranking_explanation=cand.ranking_explanation,
            rank=cand.rank,
            shortlist_rank=cand.shortlist_rank,
        )

    def _held_record(self, st: StoryState, input_key, ranking_v, verification_v, window, run_id):
        reason, detail = st.hold_reason()
        return VerificationRecord(
            **self._base(st, input_key, ranking_v, verification_v, window, run_id),
            verification_status=TECHNICAL_STATUS[reason],
            eligibility="pending",
            review_flags=[reason],
            ranking_explanation=f"Not ranked yet (technical, not an editorial decision): {detail}",
        )

    def _excluded_record(self, x: ExcludedStory, clusters, ranking_v, verification_v, window, run_id):
        cluster = next(c for c in clusters if c.cluster_id == x.story_cluster_id)
        now = self.clock()
        return VerificationRecord(
            cluster_id=x.story_cluster_id,
            cluster_revision=cluster.processing.revision,
            input_key=_hash(cluster.processing.revision, ranking_v, verification_v, window.start_date)[:64],
            ranking_config_version=ranking_v,
            verification_config_version=verification_v,
            edition_start=window.start_date,
            verification_status="insufficient_input",
            eligibility="ineligible",
            evaluated_at=now,
            expires_at=now + timedelta(days=self.settings.ranking_retention_days),
            source_count=cluster.source_count,
            review_flags=["insufficient_source_text"],
            ranking_explanation=x.detail,
            embedding_status="not_applicable",
            evaluation_status="not_applicable",
            ranking_run_id=None if self.dry_run else run_id,
        )

    # --- read side --------------------------------------------------------------------------------------
    def latest(self, *, week: str | None = None, include_incomplete: bool = True) -> RankingResult | None:
        """The most recent stored ranking (optionally for one edition week). Validated against the contract."""
        self.store.check_schema(self.settings.embedding_dimensions)
        start = None
        if week is not None:
            start = edition_for(self.clock(), week=week, timezone_name=self.config.timeliness.timezone).start_date
        statuses = None if include_incomplete else ["completed"]
        run = self.store.latest_run(edition_start=start, statuses=statuses)
        if run is None or not run.result:
            return None
        return RankingResult.model_validate(run.result)
