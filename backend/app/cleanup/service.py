"""Stage 3.6 cleanup engine: publication-gated, manifest-based, bounded and idempotent deletion of working data.

One scheduler pass (`scheduled_run`) does, in order, each step in its own short transaction:
1. Plan: create one job per confirmed-published edition that has none (unique job key + one-job-per-edition
   index, so repeated or concurrent runs never duplicate a job), and at most one orphans sweep per week.
   `not_before` = the later of published_at + safety delay and window end + minimum story age.
2. Stage: once `not_before` has passed, verify the publication again, compute every candidate's protections and
   persist the manifest (eligible, protected and skipped records, with reasons). Deletion waits for the manifest
   hold (`execute_after`).
3. Execute: claim the job (conditional UPDATE), verify the publication once more, then delete in bounded
   batches. Each batch is one transaction that locks the rows (FOR UPDATE SKIP LOCKED), checks every protection
   again, deletes, checks that the rows are gone and updates the manifest; anything that became protected in
   between is skipped, a database guard (foreign key or trigger) is never bypassed. A job is completed only after
   a final check that no deleted record is still present.
A job that fails is retried with exponential backoff up to `max_retries`; an interrupted run is resumed from the
manifest (finished records are never processed again). Every decision is written to the append-only audit log.

Nothing here calls Gemini or any other API, and nothing is decided by AI output.
"""

import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.cleanup import eligibility as el
from app.cleanup.config import RetentionConfig
from app.cleanup.publication import verify_publication
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.session import Database
from app.editions.config import EditionConfig
from app.editions.service import EDITION_JOB_KEY, Actor
from app.ingestion.store import SqlIngestionStore
from app.intelligence.store import ANALYSIS_JOB_KEY
from app.models import CleanupAuditEntry, CleanupJob, CleanupManifestItem, Edition, IngestionJob
from app.models.enums import CleanupAuditAction as A
from app.models.enums import CleanupJobKind as K
from app.models.enums import CleanupJobStatus as S
from app.models.enums import CleanupTarget as T
from app.models.enums import IngestionJobStatus, IngestionJobType
from app.models.enums import ManifestState as M
from app.processing.store import PROCESSING_JOB_KEY
from app.ranking.store import RANKING_JOB_KEY
from app.ranking.window import EditionWindow, edition_zone, monday_of

logger = logging.getLogger("app.cleanup")
CLEANUP_JOB_KEY = "stage-3.6-cleanup"
# Stages that write the working data cleanup deletes. While one of them is running, deletion waits.
PIPELINE_KEYS = (PROCESSING_JOB_KEY, ANALYSIS_JOB_KEY, RANKING_JOB_KEY, EDITION_JOB_KEY)
ACTIVE_WINDOW = timedelta(hours=72)
MAX_LOCK_ATTEMPTS = 3
CANCELLABLE = frozenset({S.SCHEDULED, S.STAGED, S.BLOCKED, S.FAILED})
OPEN = frozenset({S.SCHEDULED, S.BLOCKED, S.STAGED, S.RUNNING, S.FAILED})
RESTRICT_CODES = frozenset({"23001", "23503"})  # restrict_violation (our triggers), foreign_key_violation


class CleanupError(ApiError):
    """A safe, user-facing refusal (invalid job, state or permission). `code` is stable for the API."""

    def __init__(self, message: str, *, status_code: int = 409, code: str = "invalid_state"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class VerificationFailed(Exception):
    """Rows that were just deleted are still present: the batch is rolled back. The message holds table names
    and counts only."""

    def __init__(self, message: str):
        super().__init__(message)
        self.safe_message = f"Deletion could not be verified: {message}."


@dataclass
class RunReport:
    status: str = "ok"  # ok | disabled | skipped | partial | failed
    message: str = ""
    planned: int = 0
    staged: int = 0
    executed: list[dict] = field(default_factory=list)
    deferred: str | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def deleted(self) -> int:
        return sum(j.get("deleted", 0) for j in self.executed)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "planned": self.planned,
            "staged": self.staged,
            "deleted": self.deleted,
            "jobs": self.executed,
            "deferred": self.deferred,
            "failures": self.failures,
        }


def _safe(message: str) -> str:
    return message[:500]


class CleanupService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        config: RetentionConfig,
        edition_config: EditionConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = settings
        self.db = db
        self.config = config
        self.edition_config = edition_config
        self.clock = clock
        self.jobs = SqlIngestionStore(db)

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def _read(self) -> Iterator[Session]:
        """A session that is always rolled back: for checks and dry runs that must never write."""
        gen = self.db.read_session()
        session = next(gen)
        try:
            yield session
        finally:
            gen.close()

    @property
    def tz_name(self) -> str:
        return self.edition_config.schedule.timezone

    @property
    def start_week(self) -> date | None:
        return self.edition_config.schedule.start_week

    def _audit(
        self,
        s: Session,
        action: A,
        actor: Actor,
        *,
        job: CleanupJob | None = None,
        edition_id: uuid.UUID | None = None,
        details: dict | None = None,
    ) -> None:
        s.add(
            CleanupAuditEntry(
                job_id=job.id if job else None,
                edition_id=edition_id or (job.edition_id if job else None),
                action=action.value,
                actor_kind=actor.kind,
                actor_id=actor.user_id,
                policy_version=self.config.version,
                details=details,
                occurred_at=self.clock(),
            )
        )

    @staticmethod
    def _lock_job(s: Session, job_id: uuid.UUID) -> CleanupJob:
        job = s.scalar(select(CleanupJob).where(CleanupJob.id == job_id).with_for_update())
        if job is None:
            raise CleanupError("Cleanup job not found.", status_code=404, code="not_found")
        return job

    def _not_before(self, published_at: datetime, window: EditionWindow) -> datetime:
        pw = self.config.published_week
        return max(
            published_at + timedelta(hours=pw.safety_delay_hours), window.end + timedelta(days=pw.min_story_age_days)
        )

    def _scope(self, s: Session, job: CleanupJob) -> el.Scope:
        if job.kind == K.PUBLISHED_WEEK:
            return el.Scope(weeks=(job.window_start,))
        return el.Scope(weeks=tuple(self._cleaned_weeks(s)), before_week=self.start_week, unranked=True)

    def _cleaned_weeks(self, s: Session) -> list[date]:
        """Weeks whose published-week cleanup completed and whose edition is still confirmed published: their
        left-over records (skipped then for a temporary reason) are picked up by the orphans sweep."""
        now = self.clock()
        rows = s.execute(
            select(CleanupJob.window_start, CleanupJob.edition_id).where(
                CleanupJob.kind == K.PUBLISHED_WEEK, CleanupJob.status == S.COMPLETED
            )
        ).all()
        return sorted(week for week, edition_id in rows if verify_publication(s, edition_id, now).ok)

    def pipeline_busy(self, s: Session) -> str | None:
        key = s.scalar(
            select(IngestionJob.source_key)
            .where(IngestionJob.status == IngestionJobStatus.RUNNING, IngestionJob.source_key.in_(PIPELINE_KEYS))
            .limit(1)
        )
        return key

    # ------------------------------------------------------------------ scheduler entry point
    def scheduled_run(self, actor: Actor | None = None) -> RunReport:
        """What the daily scheduler calls. Safe to call any number of times and from several processes at once:
        one pass at a time (run lock), and every step is idempotent."""
        actor = actor or Actor.scheduler()
        report = RunReport()
        if not self.config.enabled:
            report.status, report.message = "disabled", "Cleanup is disabled in config/retention.toml."
            return report
        now = self.clock()
        self.jobs.recover_stale_jobs(now, self.settings.ingestion_job_stale_minutes)
        lock = self.jobs.start_job(CLEANUP_JOB_KEY, IngestionJobType.CLEANUP, now)
        if lock is None:
            report.status, report.message = "skipped", "Another cleanup pass is running."
            return report
        try:
            self._recover_interrupted(now)
            report.planned = self.plan(actor)
            report.staged = self.advance(actor)
            self.execute_due(actor, report)
            if report.failures:
                report.status = "failed"
            report.message = report.message or (
                f"{report.planned} job(s) planned, {report.staged} staged, {report.deleted} record(s) deleted."
            )
        except Exception as exc:  # noqa: BLE001 - recorded; never raised into the scheduler
            logger.exception("cleanup pass crashed")
            report.status, report.message = "failed", f"Cleanup pass failed ({type(exc).__name__})."
        finally:
            status = IngestionJobStatus.FAILED if report.status == "failed" else IngestionJobStatus.SUCCEEDED
            try:
                self.jobs.finish_job(
                    lock,
                    status=status,
                    now=self.clock(),
                    attempts=1,
                    details={"stage": "3.6", "trigger": actor.kind, **report.as_dict()},
                    error=report.message if status == IngestionJobStatus.FAILED else None,
                )
            except Exception:  # noqa: BLE001 - the stale-job sweep closes it later
                logger.exception("could not close the cleanup run lock")
        return report

    def _recover_interrupted(self, now: datetime) -> None:
        """A job left `running` by a crashed or stopped pass resumes from its manifest on this pass."""
        cutoff = now - timedelta(minutes=self.config.execution.stale_running_minutes)
        with self.db.write_session() as s:
            rows = s.scalars(
                select(CleanupJob)
                .where(CleanupJob.status == S.RUNNING, CleanupJob.last_run_at < cutoff)
                .with_for_update()
            ).all()
            for job in rows:
                job.status = S.STAGED
                self._audit(s, A.RETRY_SCHEDULED, Actor("system"), job=job, details={"reason": "interrupted_run"})

    # ------------------------------------------------------------------ 1. plan
    def plan(self, actor: Actor) -> int:
        now = self.clock()
        created = 0
        with self.db.write_session() as s:
            ids = s.scalars(
                select(Edition.id)
                .where(
                    Edition.status == "published",
                    Edition.is_demo.is_(False),
                    Edition.current_revision > 0,
                    ~select(CleanupJob.id)
                    .where(CleanupJob.edition_id == Edition.id, CleanupJob.kind == K.PUBLISHED_WEEK)
                    .exists(),
                )
                .order_by(Edition.week_start)
            ).all()
        for edition_id in ids:
            if self._create_week_job(edition_id, actor, now) is not None:
                created += 1
        if self.config.orphans.enabled and self._create_orphans_job(actor, now) is not None:
            created += 1
        return created

    def _create_week_job(self, edition_id: uuid.UUID, actor: Actor, now: datetime) -> uuid.UUID | None:
        try:
            with self.db.write_session() as s:
                check = verify_publication(s, edition_id, now)
                edition = s.get(Edition, edition_id)
                if edition is None or check.window is None:
                    return None
                window = check.window
                key = f"week:{window.start_date.isoformat()}"
                if s.scalar(
                    select(CleanupJob.id).where(
                        (CleanupJob.job_key == key)
                        | ((CleanupJob.edition_id == edition_id) & (CleanupJob.kind == K.PUBLISHED_WEEK))
                    )
                ):
                    return None
                published_at = edition.published_at or now
                job = CleanupJob(
                    id=uuid.uuid4(),
                    job_key=key,
                    kind=K.PUBLISHED_WEEK,
                    edition_id=edition_id,
                    window_start=window.start_date,
                    window_end=window.end_date,
                    timezone=window.timezone_name,
                    status=S.SCHEDULED if check.ok else S.BLOCKED,
                    trigger=actor.kind if actor.kind in ("admin", "cli") else "scheduler",
                    requested_by=actor.user_id,
                    policy_version=self.config.version,
                    not_before=self._not_before(published_at, window),
                    blocked_reasons=check.reasons,
                    details={"publication": check.as_dict()},
                )
                s.add(job)
                s.flush()
                self._audit(
                    s,
                    A.JOB_CREATED,
                    actor,
                    job=job,
                    details={"window": window.label, "notBefore": job.not_before.isoformat()},
                )
                self._audit(
                    s,
                    A.PUBLICATION_VERIFIED if check.ok else A.PUBLICATION_CHECK_FAILED,
                    actor,
                    job=job,
                    details=check.as_dict(),
                )
                return job.id
        except DBAPIError as exc:  # unique violation: another process created it first
            if getattr(getattr(exc, "orig", None), "sqlstate", None) == "23505":
                return None
            raise

    def _create_orphans_job(self, actor: Actor, now: datetime) -> uuid.UUID | None:
        monday = monday_of(now.astimezone(edition_zone(self.tz_name)).date())
        year, week, _ = monday.isocalendar()
        key = f"orphans:{year}-w{week:02d}"
        older_than = now - timedelta(days=self.config.orphans.min_age_days)
        try:
            with self.db.write_session() as s:
                if s.scalar(select(CleanupJob.id).where(CleanupJob.job_key == key)):
                    return None
                if s.scalar(
                    select(CleanupJob.id).where(CleanupJob.kind == K.EXPIRED_ORPHANS, CleanupJob.status.in_(OPEN))
                ):
                    return None  # the previous sweep is still in progress
                scope = el.Scope(weeks=tuple(self._cleaned_weeks(s)), before_week=self.start_week, unranked=True)
                if not (
                    el.orphan_story_candidates(s, scope, now, older_than)
                    or el.orphan_verification_candidates(s, scope, now, older_than)
                ):
                    return None
                job = CleanupJob(
                    id=uuid.uuid4(),
                    job_key=key,
                    kind=K.EXPIRED_ORPHANS,
                    timezone=self.tz_name,
                    status=S.SCHEDULED,
                    trigger=actor.kind if actor.kind in ("admin", "cli") else "scheduler",
                    requested_by=actor.user_id,
                    policy_version=self.config.version,
                    not_before=now,
                )
                s.add(job)
                s.flush()
                self._audit(s, A.JOB_CREATED, actor, job=job, details={"sweep": key})
                return job.id
        except DBAPIError as exc:
            if getattr(getattr(exc, "orig", None), "sqlstate", None) == "23505":
                return None
            raise

    # ------------------------------------------------------------------ 2. verify + stage
    def advance(self, actor: Actor) -> int:
        now = self.clock()
        with self.db.write_session() as s:
            ids = s.scalars(
                select(CleanupJob.id)
                .where(CleanupJob.status.in_((S.SCHEDULED, S.BLOCKED)))
                .order_by(CleanupJob.not_before)
            ).all()
        staged = 0
        for job_id in ids:
            if self._advance_one(job_id, actor, now):
                staged += 1
        return staged

    def _recheck(self, s: Session, job: CleanupJob, actor: Actor, now: datetime) -> bool:
        """Verify the publication of a week job again. A failure blocks the job (no deletion); a pass unblocks it.
        Audited whenever the outcome changes."""
        if job.kind != K.PUBLISHED_WEEK:
            return True
        check = verify_publication(s, job.edition_id, now)
        if check.window is not None and check.window.start_date != job.window_start:
            check.reasons.append("week_changed")
        if check.ok:
            if job.status == S.BLOCKED or job.blocked_reasons:
                self._audit(s, A.PUBLICATION_VERIFIED, actor, job=job, details=check.as_dict())
            job.blocked_reasons = []
            if job.status == S.BLOCKED:
                job.status = S.STAGED if job.staged_at else S.SCHEDULED
            return True
        if job.status != S.BLOCKED or sorted(job.blocked_reasons) != sorted(check.reasons):
            self._audit(s, A.PUBLICATION_CHECK_FAILED, actor, job=job, details=check.as_dict())
        job.status = S.BLOCKED
        job.blocked_reasons = check.reasons
        return False

    def _advance_one(self, job_id: uuid.UUID, actor: Actor, now: datetime) -> bool:
        with self.db.write_session() as s:
            job = self._lock_job(s, job_id)
            if job.status not in (S.SCHEDULED, S.BLOCKED):
                return False
            if not self._recheck(s, job, actor, now):
                return False
            if job.status != S.SCHEDULED or job.not_before > now:
                return False
            self._stage(s, job, actor, now)
            return True

    def _stage(self, s: Session, job: CleanupJob, actor: Actor, now: datetime) -> None:
        scope = self._scope(s, job)
        active_since = now - ACTIVE_WINDOW
        entries: list[tuple[str, uuid.UUID, str | None]] = []
        if job.kind == K.PUBLISHED_WEEK:
            stories = el.week_story_candidates(s, job.window_start)
            prot = el.story_protections(s, stories, scope, active_since)
            entries += [(T.STORY_CLUSTER, c, prot.get(c)) for c in stories]
            if self.config.published_week.delete_superseded_ranking_runs:
                runs = el.week_run_candidates(s, job.window_start)
                rprot = el.run_protections(s, runs, scope.weeks, active_since)
                entries += [(T.RANKING_RUN, r, rprot.get(r)) for r in runs]
            hold = self.config.published_week.manifest_hold_hours
        else:
            older_than = now - timedelta(days=self.config.orphans.min_age_days)
            stories = el.orphan_story_candidates(s, scope, now, older_than)
            prot = el.story_protections(s, stories, scope, active_since)
            entries += [(T.STORY_CLUSTER, c, prot.get(c)) for c in stories]
            orphans = el.orphan_verification_candidates(s, scope, now, older_than)
            oprot = el.orphan_verification_protections(s, orphans, scope, active_since)
            entries += [(T.ORPHAN_VERIFICATION, c, oprot.get(c)) for c in orphans]
            hold = self.config.orphans.manifest_hold_hours
        existing = {
            (t, r)
            for t, r in s.execute(
                select(CleanupManifestItem.target, CleanupManifestItem.record_id).where(
                    CleanupManifestItem.job_id == job.id
                )
            ).all()
        }
        for target, record_id, reason in entries:
            if (target, record_id) in existing:
                continue
            state = M.PENDING if reason is None else (M.SKIPPED if el.is_temporary(reason) else M.PROTECTED)
            s.add(
                CleanupManifestItem(
                    job_id=job.id,
                    target=target,
                    record_id=record_id,
                    state=state,
                    reason=reason,
                    processed_at=None if state == M.PENDING else now,
                )
            )
        s.flush()
        self._refresh_counts(s, job)
        job.status = S.STAGED
        job.staged_at = now
        job.execute_after = now + timedelta(hours=hold)
        job.policy_version = self.config.version
        self._audit(
            s,
            A.MANIFEST_STAGED,
            actor,
            job=job,
            details={
                "eligible": job.eligible_count,
                "protected": job.protected_count,
                "skipped": job.skipped_count,
                "executeAfter": job.execute_after.isoformat(),
                "reasons": job.protection_summary,
            },
        )

    @staticmethod
    def _refresh_counts(s: Session, job: CleanupJob) -> None:
        rows = s.execute(
            select(CleanupManifestItem.state, CleanupManifestItem.reason, func.count())
            .where(CleanupManifestItem.job_id == job.id)
            .group_by(CleanupManifestItem.state, CleanupManifestItem.reason)
        ).all()
        by_state: Counter = Counter()
        reasons: Counter = Counter()
        for state, reason, n in rows:
            by_state[state] += n
            if state in (M.PROTECTED, M.SKIPPED) and reason:
                reasons[reason] += n
        job.eligible_count = by_state[M.PENDING] + by_state[M.DELETED] + by_state[M.FAILED]
        job.protected_count = by_state[M.PROTECTED]
        job.skipped_count = by_state[M.SKIPPED]
        job.deleted_count = by_state[M.DELETED]
        job.failure_count = by_state[M.FAILED]
        job.protection_summary = dict(sorted(reasons.items())) or None

    # ------------------------------------------------------------------ 3. execute
    def execute_due(self, actor: Actor, report: RunReport) -> None:
        now = self.clock()
        with self.db.write_session() as s:
            busy = self.pipeline_busy(s)
            due = s.scalars(
                select(CleanupJob.id)
                .where(
                    ((CleanupJob.status == S.STAGED) & (CleanupJob.execute_after <= now))
                    | (
                        (CleanupJob.status == S.FAILED)
                        & CleanupJob.next_retry_at.is_not(None)
                        & (CleanupJob.next_retry_at <= now)
                    )
                )
                .order_by(CleanupJob.execute_after.nulls_last(), CleanupJob.created_at)
            ).all()
            if due and busy:
                for job_id in due:
                    job = s.get(CleanupJob, job_id)
                    self._audit(s, A.DEFERRED, actor, job=job, details={"reason": "pipeline_running", "stage": busy})
        if not due:
            return
        if busy:
            report.deferred = f"A pipeline stage is running ({busy}); deletion waits for the next run."
            return
        budget = self.config.execution.max_batches_per_run
        for job_id in due:
            if budget <= 0:
                break
            result = self._execute_one(job_id, actor, budget)
            budget -= result.pop("batches", 0)
            report.executed.append(result)
            if result.get("status") == S.FAILED:
                report.failures.append(f"{result.get('jobKey')}: {result.get('error')}")

    def _claim(self, job_id: uuid.UUID, actor: Actor, now: datetime) -> CleanupJob | None:
        with self.db.write_session() as s:
            job = self._lock_job(s, job_id)
            if job.status == S.FAILED:
                if job.next_retry_at is None or job.next_retry_at > now:
                    return None
                job.status = S.STAGED
                self._audit(s, A.RETRY_SCHEDULED, actor, job=job, details={"attempt": job.retry_count})
            if job.status != S.STAGED or job.execute_after is None or job.execute_after > now:
                return None
            if not self._recheck(s, job, actor, now):
                return None
            claimed = s.execute(
                update(CleanupJob)
                .where(CleanupJob.id == job_id, CleanupJob.status == S.STAGED)
                .values(status=S.RUNNING, last_run_at=now, started_at=func.coalesce(CleanupJob.started_at, now))
                .returning(CleanupJob.id)
            ).first()
            if claimed is None:
                return None
            s.refresh(job)
            self._audit(s, A.DELETION_STARTED, actor, job=job, details={"pending": self._pending_count(s, job.id)})
            return job

    @staticmethod
    def _pending_count(s: Session, job_id: uuid.UUID) -> int:
        return (
            s.scalar(
                select(func.count())
                .select_from(CleanupManifestItem)
                .where(CleanupManifestItem.job_id == job_id, CleanupManifestItem.state == M.PENDING)
            )
            or 0
        )

    def _execute_one(self, job_id: uuid.UUID, actor: Actor, budget: int) -> dict:
        now = self.clock()
        job = self._claim(job_id, actor, now)
        if job is None:
            return {"jobId": str(job_id), "status": "not_claimed", "batches": 0}
        result = {"jobId": str(job.id), "jobKey": job.job_key, "deleted": 0, "skipped": 0, "batches": 0}
        try:
            with self._read() as s:
                scope = self._scope(s, job)
            while result["batches"] < budget:
                batch = self._next_batch(job.id)
                if not batch:
                    break
                deleted, skipped = self._delete_batch(job, scope, batch, actor)
                result["batches"] += 1
                result["deleted"] += deleted
                result["skipped"] += skipped
            result["status"] = self._finish(job.id, actor)
        except Exception as exc:  # noqa: BLE001 - recorded on the job, retried with backoff
            logger.exception("cleanup job %s failed", job.job_key)
            message = _safe(getattr(exc, "safe_message", None) or f"{type(exc).__name__} during deletion.")
            result["status"], result["error"] = self._fail(job.id, actor, message), message
        return result

    def _next_batch(self, job_id: uuid.UUID) -> list[CleanupManifestItem]:
        """Story working data first, then ranking runs, a bounded batch of one target at a time."""
        size = self.config.execution.batch_size
        with self.db.write_session() as s:
            for target in (T.STORY_CLUSTER, T.ORPHAN_VERIFICATION, T.RANKING_RUN):
                rows = s.scalars(
                    select(CleanupManifestItem)
                    .where(
                        CleanupManifestItem.job_id == job_id,
                        CleanupManifestItem.state == M.PENDING,
                        CleanupManifestItem.target == target,
                    )
                    .order_by(CleanupManifestItem.record_id)
                    .limit(size)
                ).all()
                if rows:
                    for r in rows:
                        s.expunge(r)
                    return list(rows)
        return []

    def _delete_batch(self, job: CleanupJob, scope: el.Scope, batch, actor: Actor) -> tuple[int, int]:
        try:
            with self.db.write_session() as s:
                return self._delete_in_tx(s, job, scope, batch, actor)
        except DBAPIError as exc:
            if getattr(getattr(exc, "orig", None), "sqlstate", None) not in RESTRICT_CODES:
                raise
            if len(batch) == 1:
                # The database refused: a protection the application did not see. Keep the record.
                logger.warning("database protection kept %s %s", batch[0].target, batch[0].record_id)
                self._mark(job, batch, M.PROTECTED, "database_protected", actor)
                return 0, 1
        # A database guard refused part of the batch: isolate the record(s) one by one, never force anything.
        deleted = skipped = 0
        for item in batch:
            d, k = self._delete_batch(job, scope, [item], actor)
            deleted += d
            skipped += k
        return deleted, skipped

    def _delete_in_tx(self, s: Session, job: CleanupJob, scope: el.Scope, batch, actor: Actor) -> tuple[int, int]:
        now = self.clock()
        active_since = now - ACTIVE_WINDOW
        ids = [i.record_id for i in batch]
        target = batch[0].target
        items = {
            i.record_id: i
            for i in s.scalars(
                select(CleanupManifestItem)
                .where(CleanupManifestItem.id.in_([i.id for i in batch]), CleanupManifestItem.state == M.PENDING)
                .with_for_update()
            )
        }
        ids = [i for i in ids if i in items]
        if not ids:
            return 0, 0
        if target == T.RANKING_RUN:
            tables = ("ranking_runs",)
            key = "id"
        elif target == T.ORPHAN_VERIFICATION:
            tables = ("story_verifications",)
            key = "cluster_id"
        else:
            tables = ("story_verifications", "story_embeddings", "story_analyses")
            key = "cluster_id"
        present: set[uuid.UUID] = set()
        busy: set[uuid.UUID] = set()
        for table in tables:  # fixed table names, never from input
            here = {r[0] for r in s.execute(text(f"SELECT {key} FROM {table} WHERE {key} = ANY(:ids)"), {"ids": ids})}  # noqa: S608
            mine = {
                r[0]
                for r in s.execute(
                    text(f"SELECT {key} FROM {table} WHERE {key} = ANY(:ids) FOR UPDATE SKIP LOCKED"),  # noqa: S608
                    {"ids": ids},
                )
            }
            present |= here
            # Rows another transaction holds right now: tried again later, never waited for or forced.
            busy |= here - mine
        work = [i for i in ids if i not in busy]
        if target == T.RANKING_RUN:
            prot = el.run_protections(s, work, scope.weeks, active_since)
        elif target == T.ORPHAN_VERIFICATION:
            prot = el.orphan_verification_protections(s, work, scope, active_since)
        else:
            prot = el.story_protections(s, work, scope, active_since)
        eligible = [i for i in work if prot.get(i) is None]
        for table in tables:
            s.execute(text(f"DELETE FROM {table} WHERE {key} = ANY(:ids)"), {"ids": eligible})  # noqa: S608
        for table in tables:
            left = s.scalar(text(f"SELECT count(*) FROM {table} WHERE {key} = ANY(:ids)"), {"ids": eligible})  # noqa: S608
            if left:
                raise VerificationFailed(f"{left} row(s) of {table} were still present after deletion")
        skipped = 0
        for rid in ids:
            item = items[rid]
            if rid in busy:
                item.attempts += 1
                if item.attempts >= MAX_LOCK_ATTEMPTS:
                    item.state, item.reason, item.processed_at = M.SKIPPED, "locked", now
                    skipped += 1
                continue
            reason = prot.get(rid)
            item.attempts += 1
            item.processed_at = now
            if reason is None:
                item.state = M.DELETED
                item.reason = None if rid in present else "already_absent"
            else:
                item.state = M.SKIPPED if el.is_temporary(reason) else M.PROTECTED
                item.reason = reason
                skipped += 1
        locked_job = self._lock_job(s, job.id)
        self._refresh_counts(s, locked_job)
        locked_job.last_run_at = now
        self._audit(
            s,
            A.BATCH_DELETED,
            actor,
            job=locked_job,
            details={"target": target, "deleted": len(eligible), "skipped": skipped, "busy": len(busy)},
        )
        if skipped:
            self._audit(
                s,
                A.RECORDS_SKIPPED,
                actor,
                job=locked_job,
                details={"target": target, "reasons": dict(Counter(prot[i] for i in work if prot.get(i)))},
            )
        return len(eligible), skipped

    def _mark(self, job: CleanupJob, batch, state: str, reason: str, actor: Actor) -> None:
        now = self.clock()
        with self.db.write_session() as s:
            for item in batch:
                row = s.get(CleanupManifestItem, item.id, with_for_update=True)
                if row is not None and row.state == M.PENDING:
                    row.state, row.reason, row.processed_at = state, reason, now
                    row.attempts += 1
            locked_job = self._lock_job(s, job.id)
            self._refresh_counts(s, locked_job)
            self._audit(
                s,
                A.RECORDS_SKIPPED,
                actor,
                job=locked_job,
                details={"target": batch[0].target, "reasons": {reason: len(batch)}},
            )

    def _finish(self, job_id: uuid.UUID, actor: Actor) -> str:
        """Complete the job if nothing is pending and every deleted record is verifiably gone; otherwise leave it
        staged so the next run continues from the manifest."""
        now = self.clock()
        with self.db.write_session() as s:
            job = self._lock_job(s, job_id)
            self._refresh_counts(s, job)
            if self._pending_count(s, job.id):
                job.status = S.STAGED
                return S.STAGED
            leftovers = self._leftovers(s, job.id)
            if leftovers:
                raise VerificationFailed(f"{leftovers} deleted record(s) are still present")
            job.status = S.COMPLETED
            job.completed_at = now
            job.next_retry_at = None
            job.last_error = None
            self._audit(
                s,
                A.DELETION_COMPLETED,
                actor,
                job=job,
                details={
                    "deleted": job.deleted_count,
                    "protected": job.protected_count,
                    "skipped": job.skipped_count,
                    "failures": job.failure_count,
                },
            )
            return S.COMPLETED

    @staticmethod
    def _leftovers(s: Session, job_id: uuid.UUID) -> int:
        """Deleted records that still exist and were created before their deletion (a story that legitimately
        came back later, e.g. re-collected, is new working data and not a failed deletion)."""
        return (
            s.scalar(
                text(
                    """
                SELECT count(*) FROM cleanup_manifest_items m
                WHERE m.job_id = :job AND m.state = 'deleted' AND (
                    (m.target = 'ranking_run' AND EXISTS (
                        SELECT 1 FROM ranking_runs r WHERE r.id = m.record_id AND r.created_at <= m.processed_at))
                    OR (m.target IN ('story_cluster', 'orphan_verification') AND EXISTS (
                        SELECT 1 FROM story_verifications v
                        WHERE v.cluster_id = m.record_id AND v.created_at <= m.processed_at))
                    OR (m.target = 'story_cluster' AND EXISTS (
                        SELECT 1 FROM story_analyses a
                        WHERE a.cluster_id = m.record_id AND a.created_at <= m.processed_at))
                    OR (m.target = 'story_cluster' AND EXISTS (
                        SELECT 1 FROM story_embeddings e
                        WHERE e.cluster_id = m.record_id AND e.created_at <= m.processed_at))
                )
                """
                ),
                {"job": job_id},
            )
            or 0
        )

    def _fail(self, job_id: uuid.UUID, actor: Actor, message: str) -> str:
        now = self.clock()
        ex = self.config.execution
        with self.db.write_session() as s:
            job = self._lock_job(s, job_id)
            self._refresh_counts(s, job)
            job.status = S.FAILED
            job.last_error = message
            job.retry_count += 1
            if job.retry_count <= ex.max_retries:
                job.next_retry_at = now + timedelta(minutes=ex.retry_backoff_minutes * 2 ** (job.retry_count - 1))
            else:
                job.next_retry_at = None  # needs an admin retry
            self._audit(s, A.DELETION_FAILED, actor, job=job, details={"error": message, "attempt": job.retry_count})
            if job.next_retry_at:
                self._audit(
                    s, A.RETRY_SCHEDULED, actor, job=job, details={"nextRetryAt": job.next_retry_at.isoformat()}
                )
        return S.FAILED

    # ------------------------------------------------------------------ admin / CLI operations
    def _validate_id(self, value) -> uuid.UUID:
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (ValueError, TypeError):
            raise CleanupError("Invalid id.", status_code=422, code="invalid_id") from None

    def retry(self, job_id, actor: Actor) -> dict:
        job_id = self._validate_id(job_id)
        with self.db.write_session() as s:
            job = self._lock_job(s, job_id)
            if job.status != S.FAILED:
                raise CleanupError(f"Only a failed job can be retried (status is {job.status}).")
            job.status = S.STAGED if job.staged_at else S.SCHEDULED
            job.next_retry_at = None
            job.last_error = None
            self._audit(s, A.RETRY_REQUESTED, actor, job=job, details={"previousAttempts": job.retry_count})
            return self._summary(job)

    def cancel(self, job_id, actor: Actor) -> dict:
        job_id = self._validate_id(job_id)
        with self.db.write_session() as s:
            job = self._lock_job(s, job_id)
            if S(job.status) not in CANCELLABLE:
                raise CleanupError(f"A {job.status} job cannot be cancelled.")
            previous = job.status
            job.status = S.CANCELLED
            job.next_retry_at = None
            self._audit(s, A.JOB_CANCELLED, actor, job=job, details={"previousStatus": previous})
            return self._summary(job)

    def reopen(self, job_id, actor: Actor) -> dict:
        job_id = self._validate_id(job_id)
        now = self.clock()
        with self.db.write_session() as s:
            job = self._lock_job(s, job_id)
            if job.status != S.CANCELLED:
                raise CleanupError("Only a cancelled job can be reopened.")
            if job.staged_at:
                hold = (
                    self.config.published_week.manifest_hold_hours
                    if job.kind == K.PUBLISHED_WEEK
                    else self.config.orphans.manifest_hold_hours
                )
                job.status = S.STAGED
                job.execute_after = max(job.execute_after or now, now + timedelta(hours=hold))
            else:
                job.status = S.SCHEDULED
            self._recheck(s, job, actor, now)
            self._audit(s, A.JOB_REOPENED, actor, job=job, details={"status": job.status})
            return self._summary(job)

    def plan_edition(self, edition_id, actor: Actor) -> dict:
        """Manual trigger for one published edition: creates (or reopens) its job. Exactly the same checks and
        delays as the scheduler; nothing is deleted before `not_before` and the manifest hold."""
        edition_id = self._validate_id(edition_id)
        now = self.clock()
        with self.db.write_session() as s:
            edition = s.get(Edition, edition_id)
            if edition is None or edition.is_demo:
                raise CleanupError("Edition not found.", status_code=404, code="not_found")
            if edition.status != "published":
                raise CleanupError("Only a published edition's week can be cleaned up.", code="not_published")
            self._audit(s, A.MANUAL_TRIGGER, actor, edition_id=edition_id, details={"action": "plan_edition"})
            existing = s.scalar(
                select(CleanupJob).where(CleanupJob.edition_id == edition_id, CleanupJob.kind == K.PUBLISHED_WEEK)
            )
        if existing is not None:
            if existing.status == S.CANCELLED:
                return self.reopen(existing.id, actor)
            return self._summary(existing)
        job_id = self._create_week_job(edition_id, actor, now)
        with self.db.write_session() as s:
            job = s.scalar(
                select(CleanupJob).where(CleanupJob.edition_id == edition_id, CleanupJob.kind == K.PUBLISHED_WEEK)
            )
            if job is None:
                raise CleanupError("The cleanup job could not be created.", status_code=500, code="plan_failed")
            if job_id is not None:
                self._advance_one(job.id, actor, now)
                s.refresh(job)
            return self._summary(job)

    def run_now(self, actor: Actor) -> dict:
        """Manual "run cleanup now": the same pass as the scheduler (due jobs only), rate limited per admin."""
        now = self.clock()
        with self.db.write_session() as s:
            recent = s.scalar(
                select(func.count())
                .select_from(CleanupAuditEntry)
                .where(
                    CleanupAuditEntry.action == A.MANUAL_TRIGGER,
                    CleanupAuditEntry.actor_id == actor.user_id,
                    CleanupAuditEntry.occurred_at > now - timedelta(hours=1),
                    CleanupAuditEntry.details["action"].astext == "run_now",
                )
            )
            if recent >= self.config.execution.manual_runs_per_hour:
                raise CleanupError(
                    "Too many manual cleanup runs in the last hour; the scheduler runs it daily anyway.",
                    status_code=429,
                    code="rate_limited",
                )
            self._audit(s, A.MANUAL_TRIGGER, actor, details={"action": "run_now"})
        return self.scheduled_run(actor).as_dict()

    # ------------------------------------------------------------------ read
    @staticmethod
    def _summary(job: CleanupJob) -> dict:
        def ts(v):
            return v.isoformat() if v else None

        return {
            "id": str(job.id),
            "jobKey": job.job_key,
            "kind": job.kind,
            "editionId": str(job.edition_id) if job.edition_id else None,
            "windowStart": job.window_start.isoformat() if job.window_start else None,
            "windowEnd": job.window_end.isoformat() if job.window_end else None,
            "timezone": job.timezone,
            "status": job.status,
            "trigger": job.trigger,
            "policyVersion": job.policy_version,
            "notBefore": ts(job.not_before),
            "stagedAt": ts(job.staged_at),
            "executeAfter": ts(job.execute_after),
            "startedAt": ts(job.started_at),
            "lastRunAt": ts(job.last_run_at),
            "completedAt": ts(job.completed_at),
            "nextRetryAt": ts(job.next_retry_at),
            "eligible": job.eligible_count,
            "protected": job.protected_count,
            "deleted": job.deleted_count,
            "skipped": job.skipped_count,
            "failures": job.failure_count,
            "retries": job.retry_count,
            "blockedReasons": list(job.blocked_reasons or []),
            "protectionSummary": job.protection_summary or {},
            "lastError": job.last_error,
            "createdAt": ts(job.created_at),
        }

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self.db.write_session() as s:
            rows = s.execute(
                select(CleanupJob, Edition.slug)
                .outerjoin(Edition, Edition.id == CleanupJob.edition_id)
                .order_by(CleanupJob.created_at.desc())
                .limit(limit)
            ).all()
            return [{**self._summary(j), "editionSlug": slug} for j, slug in rows]

    def job_detail(self, job_id, *, state: str | None = None, limit: int = 200) -> dict:
        job_id = self._validate_id(job_id)
        if state is not None and state not in {m.value for m in M}:
            raise CleanupError("Unknown manifest state.", status_code=422, code="invalid_state_filter")
        with self.db.write_session() as s:
            job = s.get(CleanupJob, job_id)
            if job is None:
                raise CleanupError("Cleanup job not found.", status_code=404, code="not_found")
            slug = s.scalar(select(Edition.slug).where(Edition.id == job.edition_id)) if job.edition_id else None
            summary = s.execute(
                select(CleanupManifestItem.target, CleanupManifestItem.state, CleanupManifestItem.reason, func.count())
                .where(CleanupManifestItem.job_id == job_id)
                .group_by(CleanupManifestItem.target, CleanupManifestItem.state, CleanupManifestItem.reason)
                .order_by(CleanupManifestItem.target, CleanupManifestItem.state)
            ).all()
            q = select(CleanupManifestItem).where(CleanupManifestItem.job_id == job_id)
            if state:
                q = q.where(CleanupManifestItem.state == state)
            items = s.scalars(q.order_by(CleanupManifestItem.target, CleanupManifestItem.record_id).limit(limit)).all()
            audit = s.scalars(
                select(CleanupAuditEntry)
                .where(CleanupAuditEntry.job_id == job_id)
                .order_by(CleanupAuditEntry.id.desc())
                .limit(100)
            ).all()
            return {
                **self._summary(job),
                "editionSlug": slug,
                "details": job.details or {},
                "manifest": [{"target": t, "state": st, "reason": r, "count": n} for t, st, r, n in summary],
                "items": [
                    {
                        "target": i.target,
                        "recordId": str(i.record_id),
                        "state": i.state,
                        "reason": i.reason,
                        "attempts": i.attempts,
                        "processedAt": i.processed_at.isoformat() if i.processed_at else None,
                    }
                    for i in items
                ],
                "audit": [
                    {
                        "action": a.action,
                        "actorKind": a.actor_kind,
                        "actorId": str(a.actor_id) if a.actor_id else None,
                        "policyVersion": a.policy_version,
                        "details": a.details or {},
                        "occurredAt": a.occurred_at.isoformat(),
                    }
                    for a in audit
                ],
            }

    def policy(self) -> dict:
        c = self.config
        return {
            "version": c.version,
            "enabled": c.enabled,
            "publishedWeek": c.published_week.model_dump(by_alias=False),
            "orphans": c.orphans.model_dump(),
            "execution": c.execution.model_dump(),
            "activeWindowHours": int(ACTIVE_WINDOW.total_seconds() // 3600),
            "firstEditionWeek": self.start_week.isoformat() if self.start_week else None,
            "existingStage31": {
                "stagedItemRetentionDays": self.settings.ingestion_item_retention_days,
                "jobRecordRetentionDays": self.settings.ingestion_job_retention_days,
            },
            "permanent": [
                "published editions",
                "every edition revision (publication snapshots)",
                "public stories, sources and links",
                "edition items and the analysis/embedding of every story in any edition revision",
                "draft, review, approved and archived editions and their candidates",
                "the ranking run an edition was built from, and each week's latest completed run",
                "edition and cleanup audit logs, cleanup jobs and manifests",
            ],
        }

    def dry_run(self) -> dict:
        """What cleanup would do right now, without writing anything (the session is rolled back)."""
        now = self.clock()
        out: dict = {"policyVersion": self.config.version, "weeks": [], "orphans": None}
        with self._read() as s:
            active_since = now - ACTIVE_WINDOW
            editions = s.scalars(
                select(Edition)
                .where(Edition.status == "published", Edition.is_demo.is_(False), Edition.current_revision > 0)
                .order_by(Edition.week_start)
            ).all()
            for e in editions:
                check = verify_publication(s, e.id, now)
                entry: dict = {"edition": e.slug, "publication": check.as_dict()}
                if check.window is not None:
                    scope = el.Scope(weeks=(e.week_start,))
                    stories = el.week_story_candidates(s, e.week_start)
                    prot = el.story_protections(s, stories, scope, active_since)
                    runs = el.week_run_candidates(s, e.week_start)
                    rprot = el.run_protections(s, runs, scope.weeks, active_since)
                    entry.update(
                        notBefore=self._not_before(e.published_at or now, check.window).isoformat(),
                        stories={
                            "eligibleNow": sum(1 for v in prot.values() if v is None),
                            "kept": dict(Counter(v for v in prot.values() if v)),
                        },
                        rankingRuns={
                            "eligibleNow": sum(1 for v in rprot.values() if v is None),
                            "kept": dict(Counter(v for v in rprot.values() if v)),
                        },
                    )
                out["weeks"].append(entry)
            scope = el.Scope(weeks=tuple(self._cleaned_weeks(s)), before_week=self.start_week, unranked=True)
            older_than = now - timedelta(days=self.config.orphans.min_age_days)
            stories = el.orphan_story_candidates(s, scope, now, older_than)
            prot = el.story_protections(s, stories, scope, active_since)
            orphans = el.orphan_verification_candidates(s, scope, now, older_than)
            oprot = el.orphan_verification_protections(s, orphans, scope, active_since)
            out["orphans"] = {
                "stories": {
                    "eligibleNow": sum(1 for v in prot.values() if v is None),
                    "kept": dict(Counter(v for v in prot.values() if v)),
                },
                "verifications": {"eligibleNow": sum(1 for v in oprot.values() if v is None)},
            }
        return out
