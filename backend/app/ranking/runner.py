"""Stage 3.4 orchestration: lock → load the edition week's clusters and their Stage 3.3 analyses → verify and score
every story (rules) → AI consistency check for plausible shortlist candidates (cached, quota-aware) → verify and
score again with the check results → duplicate-event gate → rank → diversity-aware shortlist → explain → save.

Reliability:
- One run at a time across processes (ingestion_jobs lock). Re-running on unchanged inputs produces the same
  `run_key` and updates the same `ranking_runs` row, and cached AI checks are reused, so a duplicate execution
  costs no quota and creates no duplicate rows.
- Failure isolation: one story that cannot be evaluated or saved is listed under `failed`; the others continue.
- A story whose AI check could not run (quota, outage) is never eligible; the run is `incomplete` and the check
  is retried on a later run (backoff, at most RANKING_AI_MAX_ATTEMPTS). Quota exhaustion sends no further
  requests and raises an admin alert. No fallback model, no paid provider.
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
from app.intelligence.providers import build_provider
from app.intelligence.providers.base import IntelligenceProvider
from app.intelligence.providers.gemini import MISSING_KEY
from app.intelligence.quota import MemoryUsageLedger, UsageLedger, next_reset
from app.intelligence.runner import log_admin_alert
from app.intelligence.store import AnalysisRecord
from app.models.enums import IngestionJobStatus
from app.processing.contract import StoryCluster
from app.ranking.ai_check import AI_CHECK_VERSION, AiCheckDraft, ConsistencyChecker, check_key
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
)
from app.ranking.duplicates import apply_duplicate_gate
from app.ranking.evaluate import Evaluated, evaluate_story
from app.ranking.shortlist import explain, rank, select_shortlist
from app.ranking.store import RankingStore, RunRecord, SchemaNotReady, VerificationRecord
from app.ranking.window import EditionWindow, edition_for

logger = logging.getLogger("app.ranking")
AlertHook = Callable[[dict[str, Any]], None]
NOT_CHECKED = {"disabled", "not_selected"}


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
        provider_factory: Callable[[Settings], IntelligenceProvider] = build_provider,
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

    def versions(self, ai_enabled: bool) -> tuple[str, str]:
        ai_part = f"ai-{AI_CHECK_VERSION}" if ai_enabled else "rules"
        return self.config.ranking_version, verification_version(self.config, self.registry, ai_part)

    def run(self, *, week: str = "previous", retry_failed: bool = False, ai_check: bool | None = None) -> RankingReport:
        return asyncio.run(self.run_async(week=week, retry_failed=retry_failed, ai_check=ai_check))

    def _alert(self, report: RankingReport, kind: str, message: str, **extra) -> None:
        alert = {"kind": kind, "message": message, "stage": "3.4", "at": self.clock().isoformat(), **extra}
        report.alerts.append(alert)
        try:
            self.alert_hook(alert)
        except Exception:  # noqa: BLE001 - an alert hook must never break a run
            logger.exception("admin alert hook failed")

    async def run_async(
        self, *, week: str = "previous", retry_failed: bool = False, ai_check: bool | None = None
    ) -> RankingReport:
        now = self.clock()
        report = RankingReport(started_at=now, dry_run=self.dry_run)
        ai_enabled = self.settings.ranking_ai_check if ai_check is None else ai_check
        try:
            config, registry = self.config, self.registry
            window = edition_for(now, week=week, timezone_name=config.timeliness.timezone)
            if ai_enabled and self.settings.gemini_api_key is None:
                raise RankingConfigError(f"{MISSING_KEY} Or set RANKING_AI_CHECK=false to rank with rules only.")
            self.store.check_schema()
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
            await self._run(report, job_id, window, config, registry, ai_enabled, retry_failed)
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
        report.finished_at = self.clock()
        return report

    # ------------------------------------------------------------------------------------------------------
    async def _run(self, report, job_id, window: EditionWindow, config, registry, ai_enabled, retry_failed):
        s = self.settings
        now = report.started_at
        ranking_v, verification_v = self.versions(ai_enabled)
        clusters = [c for c in {c.cluster_id: c for c in self.clusters_source()}.values() if in_window(c, window)]
        records = {r.cluster_id: r for r in self.analyses_source()}
        stored = self.store.load_verifications([c.cluster_id for c in clusters])

        pending: list[PendingStory] = []
        excluded: list[ExcludedStory] = []
        failed: list[FailedEvaluation] = []
        ready: list[tuple[StoryCluster, StoryAnalysis, AnalysisRecord]] = []
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
                    ready.append((c, StoryAnalysis.model_validate(rec.analysis), rec))
                except Exception as exc:  # noqa: BLE001 - one unreadable analysis must not stop the run
                    failed.append(
                        FailedEvaluation(
                            story_cluster_id=c.cluster_id, error=f"stored analysis is invalid ({type(exc).__name__})"
                        )
                    )

        # AI check state per story: cached result, or why it was not run.
        ai_state: dict[uuid.UUID, dict[str, Any]] = {}
        for c, a, _ in ready:
            prev = stored.get(c.cluster_id)
            key = check_key(a, s)
            state = {
                "key": key,
                "draft": None,
                "status": "disabled" if not ai_enabled else "not_selected",
                "attempts": 0,
                "next_at": None,
                "errors": None,
            }
            if prev is not None and prev.ai_check_key == key:
                state["attempts"], state["next_at"] = prev.ai_check_attempts, prev.ai_check_next_attempt_at
                if ai_enabled and prev.ai_check and prev.ai_check_status in ("completed", "cached"):
                    try:
                        state["draft"], state["status"] = AiCheckDraft.model_validate(prev.ai_check), "cached"
                    except Exception:  # noqa: BLE001 - a bad cache entry is simply re-checked
                        state["draft"] = None
            ai_state[c.cluster_id] = state

        def evaluate_all() -> list[Evaluated]:
            out = []
            for c, a, _ in ready:
                if any(f.story_cluster_id == c.cluster_id for f in failed):
                    continue
                st = ai_state[c.cluster_id]
                try:
                    out.append(evaluate_story(c, a, window, config, registry, ai=st["draft"], ai_status=st["status"]))
                except Exception as exc:  # noqa: BLE001 - failure isolation per story
                    logger.exception("evaluation failed for cluster %s", c.cluster_id)
                    failed.append(
                        FailedEvaluation(
                            story_cluster_id=c.cluster_id, error=f"evaluation error ({type(exc).__name__})"
                        )
                    )
            apply_duplicate_gate(out, config)
            return rank(out)

        stories = evaluate_all()
        if ai_enabled:
            await self._ai_checks(report, stories, ai_state, config, retry_failed, now)
            stories = evaluate_all()

        shortlist, notes = select_shortlist(stories, config)
        positions = {e.cluster_id: i + 1 for i, e in enumerate(shortlist)}
        candidates: list[RankedCandidate] = []
        for i, e in enumerate(stories, start=1):
            candidates.append(self._candidate(e, i, positions.get(e.cluster_id)))
        for e in stories:
            st = ai_state[e.cluster_id]
            if st["status"] in ("pending", "failed", "invalid_output"):
                pending.append(
                    PendingStory(
                        story_cluster_id=e.cluster_id,
                        reason="ai_check_pending",
                        detail="AI consistency check waiting for the Gemini free quota."
                        if st["status"] == "pending"
                        else f"AI consistency check {st['status'].replace('_', ' ')}; retried on a later run.",
                    )
                )
        counts = Counter(c.eligibility_status for c in candidates)
        status = "incomplete" if pending else "completed"
        reason = None
        if pending:
            why = Counter(p.reason for p in pending)
            reason = "Not a final ranking: " + ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in why.items())
        if not ready and not clusters:
            notes.append("no stories were collected in this edition week")
        by_id = {c.story_cluster_id: c for c in candidates}
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
            ai_check={
                "enabled": ai_enabled,
                "model": s.ai_model if ai_enabled else None,
                "statuses": dict(Counter(st["status"] for st in ai_state.values())),
                **report.ai_usage,
            },
            notes=notes,
            dry_run=self.dry_run,
        )
        input_keys = {
            c.story_cluster_id: _hash(
                c.cluster_revision,
                c.analysis_generated_at.isoformat(),
                ai_state[c.story_cluster_id]["key"],
                "checked"
                if c.verification.ai_check_status in ("completed", "cached")
                else c.verification.ai_check_status,
                ranking_v,
                verification_v,
                window.start_date,
            )[:64]
            for c in candidates
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
        # Save every story; a failed write is isolated and reported.
        for e, cand in zip(stories, candidates, strict=True):
            st = ai_state[e.cluster_id]
            try:
                self.store.save_verification(
                    self._record(e, cand, st, input_keys[e.cluster_id], ranking_v, verification_v, window, run_id)
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("could not save verification for %s", e.cluster_id)
                failed.append(
                    FailedEvaluation(story_cluster_id=e.cluster_id, error=f"save failed ({type(exc).__name__})")
                )
        for x in excluded:
            try:
                self.store.save_verification(
                    self._excluded_record(x, clusters, ranking_v, verification_v, window, run_id)
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    FailedEvaluation(story_cluster_id=x.story_cluster_id, error=f"save failed ({type(exc).__name__})")
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

    async def _ai_checks(self, report, stories: list[Evaluated], ai_state, config, retry_failed, now) -> None:
        s = self.settings
        rules_only_blockers = {"verification_incomplete"}
        wanted = []
        for e in stories:
            st = ai_state[e.cluster_id]
            if st["draft"] is not None:
                continue
            blocked = [g for g in e.gates if g.gate not in rules_only_blockers and g.action != "eligible"]
            if blocked or e.final < config.ai_check.min_score:
                st["status"] = "not_selected"
                continue
            if not retry_failed and st["attempts"] >= s.ranking_ai_max_attempts:
                st["status"] = "failed"
                continue
            if not retry_failed and st["next_at"] and st["next_at"] > now:
                st["status"] = "failed"
                continue
            wanted.append(e)
        report.ai_usage = {"requested": len(wanted), "checked": 0, "calls": 0, "inputTokens": 0, "outputTokens": 0}
        if not wanted:
            return
        snapshot = self.ledger.snapshot(s.ai_model, now)
        if snapshot.quota_exhausted_until is not None:
            for e in wanted:
                ai_state[e.cluster_id]["status"] = "pending"
            self._alert(
                report,
                "quota_exhausted",
                f"The Gemini free quota for {s.ai_model} is used up until "
                f"{snapshot.quota_exhausted_until.isoformat()}; AI checks for {len(wanted)} stories wait.",
            )
            return
        budget = s.ranking_ai_max_checks_per_run
        for e in wanted[budget:]:
            ai_state[e.cluster_id]["status"] = "pending"
        todo = wanted[:budget]
        if not todo:
            return
        provider = self.provider_factory(s)
        checker = ConsistencyChecker(s, provider, ledger=self.ledger, sleep=self.sleep, clock=self.clock)
        semaphore = asyncio.Semaphore(s.ai_max_concurrency)
        stop: list[str] = []

        async def one(e: Evaluated) -> None:
            st = ai_state[e.cluster_id]
            async with semaphore:
                if stop:
                    st["status"] = "pending"
                    return
                outcome = await checker.check(e.analysis, e.outcome.pack)
            u = report.ai_usage
            u["calls"] += outcome.calls
            u["inputTokens"] += outcome.usage.input_tokens
            u["outputTokens"] += outcome.usage.output_tokens
            if outcome.status == "completed":
                st.update(
                    draft=outcome.draft, status="completed", attempts=st["attempts"] + 1, next_at=None, errors=None
                )
                u["checked"] += 1
                return
            if outcome.status == "quota_limited" or outcome.fatal:
                stop.append(outcome.error.safe_message if outcome.error else "stopped")
                st["status"] = "pending"
                st["errors"] = [{"code": f"provider_{outcome.error.kind}", "message": outcome.error.safe_message}]
                return
            attempts = st["attempts"] + 1
            st.update(
                status="invalid_output" if outcome.status == "invalid_output" else "failed",
                attempts=attempts,
                next_at=self.clock() + timedelta(minutes=s.ai_retry_delay_minutes * attempts),
                errors=[{"code": f"provider_{outcome.error.kind}", "message": outcome.error.safe_message}]
                if outcome.error
                else [{"code": "invalid_output", "message": p[:200]} for p in outcome.problems[:5]],
            )

        try:
            await asyncio.gather(*(one(e) for e in todo))
        finally:
            await provider.aclose()
        if stop:
            kind = "quota_exhausted" if checker.quota_stopped else "provider_error"
            waiting = sum(1 for st in ai_state.values() if st["status"] == "pending")
            self._alert(report, kind, stop[0], waiting_stories=waiting, resumes_after=next_reset(now).isoformat())

    def _candidate(self, e: Evaluated, position: int, shortlist_rank: int | None) -> RankedCandidate:
        return RankedCandidate(
            story_cluster_id=e.cluster_id,
            headline=e.analysis.headline,
            primary_category=e.analysis.primary_category,
            primary_category_key=e.analysis.primary_category_key,
            organisation=e.organisation,
            coverage_groups=e.coverage_groups,
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

    def _record(self, e, cand: RankedCandidate, st, input_key, ranking_v, verification_v, window, run_id):
        v = e.verification
        now = self.clock()
        scores = cand.final_scores
        return VerificationRecord(
            cluster_id=e.cluster_id,
            cluster_revision=e.cluster.processing.revision,
            input_key=input_key,
            ranking_config_version=ranking_v,
            verification_config_version=verification_v,
            edition_start=window.start_date,
            verification_status=v.verification_status,
            eligibility=e.eligibility,
            evaluated_at=now,
            expires_at=now + timedelta(days=self.settings.ranking_retention_days),
            analysis_generated_at=e.analysis.generation.generated_at,
            source_credibility_score=v.source_credibility_score,
            source_credibility_level=v.source_credibility_level,
            evidence_quality_score=v.evidence_quality_score,
            claim_consistency_score=v.claim_consistency_score,
            technical_substance_score=v.technical_substance_score,
            corroboration_level=v.corroboration_level,
            source_count=v.source_count,
            independent_source_count=v.independent_source_count,
            final_priority_score=scores.final_priority_score,
            priority_classification=cand.priority_classification,
            dimension_scores=scores.model_dump(mode="json", by_alias=True),
            verification=v.model_dump(mode="json", by_alias=True),
            review_flags=cand.review_flags,
            ranking_explanation=cand.ranking_explanation,
            rank=cand.rank,
            shortlist_rank=cand.shortlist_rank,
            ai_check_status=st["status"],
            ai_check_key=st["key"],
            ai_check=st["draft"].model_dump(mode="json") if st["draft"] is not None else None,
            ai_check_attempts=st["attempts"],
            ai_check_next_attempt_at=st["next_at"],
            errors=st["errors"],
            ranking_run_id=None if self.dry_run else run_id,
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
            ai_check_status="disabled",
            ranking_run_id=None if self.dry_run else run_id,
        )

    # --- read side --------------------------------------------------------------------------------------
    def latest(self, *, week: str | None = None, include_incomplete: bool = True) -> RankingResult | None:
        """The most recent stored ranking (optionally for one edition week). Validated against the contract."""
        self.store.check_schema()
        start = None
        if week is not None:
            start = edition_for(self.clock(), week=week, timezone_name=self.config.timeliness.timezone).start_date
        statuses = None if include_incomplete else ["completed"]
        run = self.store.latest_run(edition_start=start, statuses=statuses)
        if run is None or not run.result:
            return None
        return RankingResult.model_validate(run.result)
