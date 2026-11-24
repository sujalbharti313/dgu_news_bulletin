"""Stage 3.5 workflow: weekly generation, revisions, review, approval and controlled publishing.

Every operation runs in one database transaction that locks the edition row (`SELECT ... FOR UPDATE`), checks the
state machine (app.editions.states) and the caller's `expected_revision` (optimistic concurrency: a stale browser
tab can never approve or publish something it has not seen), writes an append-only revision and audit entries,
and commits all or nothing. Network calls to Gemini never happen inside a transaction.

Generation is idempotent: one edition per week (unique week_start), one generation at a time (ingestion_jobs lock
`stage-3.5-edition`), and a scheduler run that finds an existing edition does nothing. Failures are persisted on
the edition (generation_failed, safe error text, attempt count, next attempt time) and retried with exponential
backoff up to `generation.max_attempts`; an admin can retry at any time. A used-up Gemini quota is not a failure
of the edition: it waits for the quota reset and does not use up an attempt.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import Database
from app.editions import errors
from app.editions.compose import (
    PROMPT_VERSION,
    CompositionInput,
    CompositionResult,
    EditionComposer,
    fallback_introduction,
    fallback_title,
)
from app.editions.config import EditionConfig
from app.editions.content import (
    EDITORIAL_FIELDS,
    EditionContent,
    EditionStory,
    Issue,
    StoryText,
    clean_text,
    content_hash,
    has_errors,
    public_story_fields,
    validate_content,
)
from app.editions.schedule import due_weeks, edition_slug
from app.editions.selection import SELECTABLE, addable_candidates, build_story, select_candidates
from app.editions.states import EDITABLE, REGENERATABLE, after_edit, allowed_actions, transition
from app.ingestion.store import SqlIngestionStore
from app.intelligence.contract import StoryAnalysis
from app.models import (
    Edition,
    EditionAuditEntry,
    EditionItem,
    EditionRevision,
    EditorialAdmin,
    RankingRun,
    Source,
    Story,
    StoryAnalysisRecord,
)
from app.models.enums import EditionAuditAction as A
from app.models.enums import EditionStatus as S
from app.models.enums import IngestionJobStatus, IngestionJobType
from app.ranking.contract import RankedCandidate, RankingResult
from app.ranking.window import EditionWindow
from app.services.urls import canonicalize_url

logger = logging.getLogger("app.editions")
EDITION_JOB_KEY = "stage-3.5-edition"
USABLE_ANALYSIS = ("completed", "needs_review")
SOURCE_KIND_MAP = {
    "official_channel": "official_blog",
    "research_repository": "research_repository",
    "government": "government",
    "news_outlet": "news_site",
    "newsletter": "newsletter",
}
MONTHS = ("January February March April May June July August September October November December").split()


@dataclass(frozen=True)
class Actor:
    kind: str  # admin | scheduler | cli | system
    user_id: uuid.UUID | None = None

    @staticmethod
    def scheduler() -> "Actor":
        return Actor("scheduler")


@dataclass
class GenerationReport:
    week_start: date
    status: str  # generated | skipped | waiting | failed | quota_limited
    message: str
    edition_id: uuid.UUID | None = None
    edition_status: str | None = None
    revision: int | None = None

    def as_dict(self) -> dict:
        return {
            "weekStart": self.week_start.isoformat(),
            "status": self.status,
            "message": self.message,
            "editionId": str(self.edition_id) if self.edition_id else None,
            "editionStatus": self.edition_status,
            "revision": self.revision,
        }


@dataclass
class StoryInput:
    """One story of an admin revision. Only editorial text is taken from the client."""

    cluster_id: uuid.UUID
    text: StoryText


@dataclass
class RevisionInput:
    expected_revision: int
    title: str
    introduction: str
    editorial_note: str
    stories: list[StoryInput]
    acknowledge_review: set[uuid.UUID] = field(default_factory=set)


def week_label(window: EditionWindow) -> str:
    a, b = window.start_date, window.end_date
    if a.month == b.month:
        return f"{a.day}–{b.day} {MONTHS[a.month - 1]} {a.year}"
    if a.year == b.year:
        return f"{a.day} {MONTHS[a.month - 1]} – {b.day} {MONTHS[b.month - 1]} {b.year}"
    return f"{a.day} {MONTHS[a.month - 1]} {a.year} – {b.day} {MONTHS[b.month - 1]} {b.year}"


def week_numbers(window: EditionWindow) -> set[str]:
    a, b = window.start_date, window.end_date
    return {str(x) for x in (a.day, b.day, a.month, b.month, a.year, b.year)}


def _issue_dicts(issues: list[Issue]) -> list[dict]:
    return [i.model_dump() for i in issues]


class EditionService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        config: EditionConfig,
        *,
        composer_factory: Callable[[], EditionComposer | None] | None = None,
        rank_week: Callable[[date], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = settings
        self.db = db
        self.config = config
        self.composer_factory = composer_factory or (lambda: None)
        self.rank_week = rank_week
        self.clock = clock
        self.jobs = SqlIngestionStore(db)

    # ------------------------------------------------------------------ helpers
    def _window(self, week_start: date) -> EditionWindow:
        return EditionWindow(week_start, self.config.schedule.timezone)

    @staticmethod
    def _lock(s: Session, edition_id: uuid.UUID) -> Edition:
        edition = s.scalar(
            select(Edition).where(Edition.id == edition_id, Edition.is_demo.is_(False)).with_for_update()
        )
        if edition is None:
            raise errors.not_found()
        return edition

    @staticmethod
    def _audit(
        s: Session,
        edition_id: uuid.UUID | None,
        action: A,
        actor: Actor,
        *,
        revision: int | None = None,
        details: dict | None = None,
    ) -> None:
        s.add(
            EditionAuditEntry(
                edition_id=edition_id,
                revision=revision,
                action=action.value,
                actor_kind=actor.kind,
                actor_id=actor.user_id,
                details=details,
            )
        )

    @staticmethod
    def _current_revision(s: Session, edition: Edition) -> EditionRevision | None:
        if not edition.current_revision:
            return None
        return s.scalar(
            select(EditionRevision).where(
                EditionRevision.edition_id == edition.id, EditionRevision.revision == edition.current_revision
            )
        )

    @staticmethod
    def _content(rev: EditionRevision | None) -> EditionContent | None:
        return EditionContent.model_validate(rev.content) if rev is not None else None

    @staticmethod
    def _check_expected(edition: Edition, expected: int) -> None:
        if edition.current_revision != expected:
            raise errors.stale(edition.current_revision)

    @staticmethod
    def _ranking(
        s: Session,
        *,
        week_start: date | None = None,
        run_id: uuid.UUID | None = None,
        finalized_after: datetime | None = None,
        statuses: tuple[str, ...] = ("completed",),
    ):
        """A ranking run by id, or the latest COMPLETED run of a week made after `finalized_after` (the end
        of the collection window: a ranking made while the week was still running is not final)."""
        q = select(RankingRun)
        if run_id is not None:
            q = q.where(RankingRun.id == run_id)
        else:
            q = q.where(RankingRun.edition_start == week_start, RankingRun.status.in_(statuses))
            if finalized_after is not None:
                q = q.where(RankingRun.processed_at >= finalized_after)
        row = s.scalar(q.order_by(RankingRun.processed_at.desc()).limit(1))
        if row is None or not row.result:
            return None, None
        return row, RankingResult.model_validate(row.result)

    @staticmethod
    def _analyses(s: Session, cluster_ids: list[uuid.UUID]) -> dict[uuid.UUID, StoryAnalysis]:
        if not cluster_ids:
            return {}
        rows = s.scalars(
            select(StoryAnalysisRecord).where(
                StoryAnalysisRecord.cluster_id.in_(cluster_ids), StoryAnalysisRecord.status.in_(USABLE_ANALYSIS)
            )
        ).all()
        return {r.cluster_id: StoryAnalysis.model_validate(r.analysis) for r in rows if r.analysis}

    def _validate(self, content: EditionContent) -> list[Issue]:
        issues = validate_content(content, self.config.limits, min_stories=self.config.selection.min_stories)
        if content.stories and content.composition.get("mode") == "stage33_text":
            issues.append(
                Issue(
                    severity="warning",
                    code="not_ai_composed",
                    message="Gemini could not compose this edition; the text is Stage 3.3's, unchanged, and the "
                    "introduction was built from the story list. Review it before approving.",
                    path="introduction",
                )
            )
        pending = content.composition.get("rankingPending")
        if pending:
            issues.append(
                Issue(
                    severity="warning",
                    code="incomplete_ranking",
                    message=f"Built from an incomplete ranking: {pending} of this week's stories had not been "
                    "analysed yet, so a strong story may be missing. Check the candidates before approving.",
                    path="stories",
                )
            )
        return issues

    def _db_issues(self, s: Session, edition: Edition, content: EditionContent) -> list[Issue]:
        """Checks every story against backend records: the analysis exists, the source link is one of the
        story's recorded source URLs, the story is a selectable candidate of its ranking run, and the article
        has not been published in another edition."""
        issues: list[Issue] = []
        ids = [st.cluster_id for st in content.stories]
        analyses = self._analyses(s, ids)
        rankings: dict[uuid.UUID, RankingResult | None] = {}
        for i, st in enumerate(content.stories):
            path = f"stories[{i}]"
            analysis = analyses.get(st.cluster_id)
            if analysis is None:
                issues.append(
                    Issue(
                        severity="error",
                        code="source_record_missing",
                        message="The Stage 3.3 record of this story no longer exists or is not usable.",
                        path=path,
                    )
                )
                continue
            if st.source_url not in {src.original_url for src in analysis.sources}:
                issues.append(
                    Issue(
                        severity="error",
                        code="source_url_unverified",
                        message="The source link does not match the story's recorded sources.",
                        path=path,
                    )
                )
            if analysis.cluster_revision != st.cluster_revision:
                issues.append(
                    Issue(
                        severity="warning",
                        code="story_updated_since_selection",
                        message="New reports were added to this story after it was selected; check the summary.",
                        path=path,
                    )
                )
            run_id = st.selection.ranking_run_id
            if run_id is not None and run_id not in rankings:
                rankings[run_id] = self._ranking(s, run_id=run_id)[1]
            ranking = rankings.get(run_id) if run_id else None
            if ranking is not None:
                cand = next((c for c in ranking.candidates if c.story_cluster_id == st.cluster_id), None)
                if cand is None or cand.eligibility_status not in SELECTABLE:
                    issues.append(
                        Issue(
                            severity="error",
                            code="not_a_candidate",
                            message="This story is not an eligible candidate of the edition's Stage 3.4 ranking.",
                            path=path,
                        )
                    )
                elif cand.eligibility_status != st.selection.eligibility:
                    issues.append(
                        Issue(
                            severity="error",
                            code="eligibility_changed",
                            message="The story's Stage 3.4 eligibility differs from what was selected.",
                            path=path,
                        )
                    )
            try:
                canonical = canonicalize_url(st.source_url)
            except ValueError:
                continue
            other = s.scalar(
                select(Edition.slug)
                .join(Story, Story.edition_id == Edition.id)
                .where(Story.canonical_url == canonical, Story.edition_id != edition.id)
                .limit(1)
            )
            if other:
                issues.append(
                    Issue(
                        severity="error",
                        code="already_published",
                        message=f"This article is already published in edition {other}.",
                        path=path,
                    )
                )
        return issues

    def _write_revision(
        self,
        s: Session,
        edition: Edition,
        content: EditionContent,
        *,
        origin: str,
        actor: Actor,
        changes: dict | None,
        issues: list[Issue],
    ) -> EditionRevision:
        number = edition.current_revision + 1
        rev = EditionRevision(
            edition_id=edition.id,
            revision=number,
            origin=origin,
            content=content.model_dump(mode="json"),
            content_hash=content_hash(content),
            changes=changes,
            issues=_issue_dicts(issues) or None,
            created_by=actor.user_id,
        )
        s.add(rev)
        s.execute(delete(EditionItem).where(EditionItem.edition_id == edition.id))
        s.flush()
        for pos, st in enumerate(content.stories, start=1):
            s.add(
                EditionItem(
                    edition_id=edition.id,
                    cluster_id=st.cluster_id,
                    position=pos,
                    revision=number,
                    selection_source=st.selection.source,
                    eligibility=st.selection.eligibility,
                    ranking_rank=st.selection.rank,
                    shortlist_rank=st.selection.shortlist_rank,
                    final_priority_score=st.selection.final_priority_score,
                    review_acknowledged_by=st.selection.review_acknowledged_by,
                )
            )
        edition.current_revision = number
        edition.title = content.title[:200] or edition.title
        edition.introduction = content.introduction or None
        edition.editorial_note = content.editorial_note or None
        self._audit(
            s,
            edition.id,
            A.REVISION_CREATED,
            actor,
            revision=number,
            details={"origin": origin, "contentHash": rev.content_hash, "stories": len(content.stories)},
        )
        return rev

    # ------------------------------------------------------------------ generation
    def scheduled_run(self) -> list[GenerationReport]:
        """What the weekly scheduler calls. Safe to call any number of times: only due weeks without an
        edition (or with a failed generation whose backoff has passed) are worked on."""
        now = self.clock()
        reports = []
        for window in due_weeks(now, self.config.schedule):
            reports.append(self.generate_week(window.start_date, actor=Actor.scheduler()))
        return reports

    def generate_week(self, week_start: date, *, actor: Actor, force: bool = False) -> GenerationReport:
        window = self._window(week_start)
        now = self.clock()
        if now < window.end:
            return GenerationReport(week_start, "skipped", f"The week {window.label} has not ended yet.")
        self.jobs.recover_stale_jobs(now, self.settings.ingestion_job_stale_minutes)
        job_id = self.jobs.start_job(EDITION_JOB_KEY, IngestionJobType.EDITION_GENERATION, now)
        if job_id is None:
            return GenerationReport(week_start, "skipped", "Another edition generation is running.")
        report = GenerationReport(week_start, "failed", "Generation did not finish.")
        try:
            report = self._generate(window, actor=actor, force=force, job_id=job_id)
        except errors.EditionError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded on the edition and the job; never leaks details
            logger.exception("edition generation crashed for %s", window.label)
            report = self._record_failure(window, job_id, actor, f"Unexpected error ({type(exc).__name__}).")
        finally:
            job_status = {
                "generated": IngestionJobStatus.SUCCEEDED,
                "skipped": IngestionJobStatus.SKIPPED,
                "waiting": IngestionJobStatus.SKIPPED,
                "quota_limited": IngestionJobStatus.SKIPPED,
            }.get(report.status, IngestionJobStatus.FAILED)
            try:
                self.jobs.finish_job(
                    job_id,
                    status=job_status,
                    now=self.clock(),
                    attempts=1,
                    details={"stage": "3.5", "trigger": actor.kind, **report.as_dict()},
                    error=report.message if job_status == IngestionJobStatus.FAILED else None,
                )
            except Exception:  # noqa: BLE001 - the stale-job sweep closes it later
                logger.exception("could not close the edition generation job")
        return report

    def _claim(self, window: EditionWindow, *, actor: Actor, force: bool, job_id: uuid.UUID, ranking_run_id):
        """Create the edition (or move a failed/regeneratable one) to `generating`. Returns (edition_id, None) or
        (None, report) when there is nothing to do."""
        now = self.clock()
        gen = self.config.generation
        with self.db.write_session() as s:
            edition = s.scalar(
                select(Edition)
                .where(Edition.week_start == window.start_date, Edition.is_demo.is_(False))
                .with_for_update()
            )
            if edition is None:
                number = (s.scalar(select(func.max(Edition.number)).where(Edition.is_demo.is_(False))) or 0) + 1
                edition = Edition(
                    slug=edition_slug(window),
                    number=number,
                    title=fallback_title(week_label(window)),
                    week_start=window.start_date,
                    week_end=window.end_date,
                    status=S.GENERATING.value,
                    timezone=window.timezone_name,
                    is_demo=False,
                )
                s.add(edition)
                s.flush()
                action = A.GENERATION_STARTED
            else:
                status = S(edition.status)
                stale_generation = status is S.GENERATING and not self._job_running(s, edition.generation_job_id)
                if status is S.GENERATION_FAILED or stale_generation:
                    if actor.kind == "scheduler":
                        if edition.generation_attempts >= gen.max_attempts:
                            return None, GenerationReport(
                                window.start_date,
                                "skipped",
                                f"Generation failed {edition.generation_attempts} times; an admin must retry it.",
                                edition.id,
                                edition.status,
                            )
                        if edition.next_attempt_at and edition.next_attempt_at > now:
                            return None, GenerationReport(
                                window.start_date,
                                "skipped",
                                f"Waiting until {edition.next_attempt_at.isoformat()} before retrying.",
                                edition.id,
                                edition.status,
                            )
                    if status is S.GENERATION_FAILED:
                        edition.status = transition(edition.status, S.GENERATING)
                    action = A.RETRY_TRIGGERED
                elif force and status in REGENERATABLE:
                    edition.status = transition(edition.status, S.GENERATING)
                    action = A.REGENERATED
                else:
                    return None, GenerationReport(
                        window.start_date,
                        "skipped",
                        f"An edition for this week already exists ({edition.status}).",
                        edition.id,
                        edition.status,
                        edition.current_revision,
                    )
            edition.generation_attempts += 1
            edition.generation_job_id = job_id
            edition.ranking_run_id = ranking_run_id
            self._audit(
                s,
                edition.id,
                action,
                actor,
                revision=edition.current_revision or None,
                details={"week": window.as_dict(), "attempt": edition.generation_attempts},
            )
            return edition.id, None

    @staticmethod
    def _job_running(s: Session, job_id: uuid.UUID | None) -> bool:
        if job_id is None:
            return False
        from app.models import IngestionJob

        return s.scalar(select(IngestionJob.status).where(IngestionJob.id == job_id)) == IngestionJobStatus.RUNNING

    def _generate(self, window: EditionWindow, *, actor: Actor, force: bool, job_id: uuid.UUID) -> GenerationReport:
        cfg = self.config
        # 1. A finished Stage 3.4 ranking for exactly this week (an incomplete run is not a final ranking).
        with self.db.write_session() as s:
            existing = s.scalar(
                select(Edition).where(Edition.week_start == window.start_date, Edition.is_demo.is_(False))
            )
            if existing is not None and not force and existing.status not in (S.GENERATION_FAILED, S.GENERATING):
                return GenerationReport(
                    window.start_date,
                    "skipped",
                    f"An edition for this week already exists ({existing.status}).",
                    existing.id,
                    existing.status,
                    existing.current_revision,
                )
            run, ranking = self._ranking(s, week_start=window.start_date, finalized_after=window.end)
        if ranking is None and cfg.schedule.run_ranking_if_missing and self.rank_week is not None:
            logger.info("no completed ranking for %s; running Stage 3.4 first", window.label)
            try:
                self.rank_week(window.start_date)
            except Exception:  # noqa: BLE001 - reported below as "waiting"
                logger.exception("Stage 3.4 run for %s failed", window.label)
            with self.db.write_session() as s:
                run, ranking = self._ranking(s, week_start=window.start_date, finalized_after=window.end)
        partial_pending: int | None = None
        grace = cfg.schedule.accept_incomplete_ranking_after_hours
        if ranking is None and grace and self.clock() >= window.end + timedelta(hours=grace):
            with self.db.write_session() as s:
                run, ranking = self._ranking(
                    s,
                    week_start=window.start_date,
                    finalized_after=window.end,
                    statuses=("completed", "incomplete"),
                )
            if ranking is not None and ranking.shortlist:
                partial_pending = len(ranking.pending)
                logger.info(
                    "using incomplete ranking for %s (%d stories still pending) after %dh grace",
                    window.label,
                    partial_pending,
                    grace,
                )
            else:
                run, ranking = None, None
        if ranking is None:
            with self.db.write_session() as s:
                self._audit(
                    s,
                    existing.id if existing else None,
                    A.GENERATION_DEFERRED,
                    actor,
                    details={"week": window.as_dict(), "reason": "no final completed Stage 3.4 ranking"},
                )
            return GenerationReport(
                window.start_date,
                "waiting",
                "No completed Stage 3.4 ranking made after the week ended exists yet (some stories may still be "
                "pending). Generation will be retried on the next scheduler run.",
                existing.id if existing else None,
                existing.status if existing else None,
            )

        # 2. Claim the edition row.
        edition_id, skipped = self._claim(window, actor=actor, force=force, job_id=job_id, ranking_run_id=run.id)
        if skipped is not None:
            return skipped

        # 3. Select and build the stories from backend records.
        picked = select_candidates(ranking, cfg.selection.target_stories)
        with self.db.write_session() as s:
            analyses = self._analyses(s, [c.story_cluster_id for c in picked.candidates])
        stories: list[EditionStory] = []
        skipped_list = list(picked.skipped)
        context: dict[str, dict] = {}
        for cand in picked.candidates:
            analysis = analyses.get(cand.story_cluster_id)
            if analysis is None or analysis.cluster_revision != cand.cluster_revision:
                skipped_list.append(
                    {"clusterId": str(cand.story_cluster_id), "reason": "Stage 3.3 analysis missing or changed"}
                )
                continue
            story = build_story(analysis, cand, source="ranking", eligibility="eligible", ranking_run_id=run.id)
            if story is None:
                skipped_list.append({"clusterId": str(cand.story_cluster_id), "reason": "no source with a valid URL"})
                continue
            stories.append(story)
            context[f"E{len(stories)}"] = {"key_points": list(analysis.understanding.key_points)[:6]}

        # 4. Compose (outside any transaction).
        label = week_label(window)
        composition = self._compose(stories, window, label, context)
        if composition.status == "quota_limited":
            return self._defer_for_quota(window, edition_id, job_id, actor, composition)
        mode = "ai" if composition.status == "completed" else "stage33_text"
        if mode != "ai" and stories and cfg.generation.use_ai and not cfg.generation.fallback_to_stage33_text:
            return self._record_failure(
                window, job_id, actor, composition.error or "Gemini could not compose the edition.", edition_id
            )
        if mode == "ai":
            refs = {f"E{i + 1}": s for i, s in enumerate(stories)}
            stories = [s.model_copy(update={"text": composition.texts.get(ref, s.text)}) for ref, s in refs.items()]
        content = EditionContent(
            title=(composition.title if mode == "ai" else None) or fallback_title(label),
            introduction=(composition.introduction if mode == "ai" else None) or fallback_introduction(stories, label),
            editorial_note=(composition.closing_note if mode == "ai" else "") or "",
            week_start=window.start_date,
            week_end=window.end_date,
            timezone=window.timezone_name,
            stories=stories,
            composition={
                "mode": mode,
                "model": composition.model if composition.calls else None,
                "promptVersion": PROMPT_VERSION,
                "configVersion": cfg.version,
                "rankingRunId": str(run.id),
                "composedAt": self.clock().isoformat(),
                **({"rankingPending": partial_pending} if partial_pending is not None else {}),
            },
        )

        # 5. Store the revision and move to review.
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            if edition.status != S.GENERATING or edition.generation_job_id != job_id:
                return GenerationReport(
                    window.start_date, "skipped", "The edition was changed by another process.", edition.id
                )
            issues = self._validate(content) + self._db_issues(s, edition, content)
            origin = "generated" if edition.current_revision == 0 else "regenerated"
            rev = self._write_revision(s, edition, content, origin=origin, actor=actor, changes=None, issues=issues)
            edition.status = transition(edition.status, S.DRAFT if has_errors(issues) else S.REVIEW_REQUIRED)
            edition.generated_at = self.clock()
            edition.next_attempt_at = None
            edition.last_error = None
            edition.generation = {
                "mode": mode,
                "model": composition.model if composition.calls else None,
                "modelVersion": composition.model_version,
                "promptVersion": PROMPT_VERSION,
                "calls": composition.calls,
                "inputTokens": composition.usage.input_tokens,
                "outputTokens": composition.usage.output_tokens,
                "aiProblems": composition.problems[:10],
                "aiError": composition.error,
                "shortlisted": len(ranking.shortlist),
                "selected": len(stories),
                "skipped": skipped_list[:20],
                "rankingRunId": str(run.id),
                "configVersion": cfg.version,
            }
            self._audit(
                s,
                edition.id,
                A.GENERATED,
                actor,
                revision=rev.revision,
                details={"mode": mode, "stories": len(stories), "status": edition.status, "origin": origin},
            )
            if edition.status == S.REVIEW_REQUIRED:
                self._audit(s, edition.id, A.APPROVAL_REQUESTED, actor, revision=rev.revision)
            return GenerationReport(
                window.start_date,
                "generated",
                f"Edition {edition.slug} generated with {len(stories)} stories ({mode}); status {edition.status}.",
                edition.id,
                edition.status,
                rev.revision,
            )

    def _compose(self, stories, window, label, context) -> CompositionResult:
        if not stories or not self.config.generation.use_ai:
            return CompositionResult("skipped")
        composer = None
        try:
            composer = self.composer_factory()
        except Exception as exc:  # noqa: BLE001 - e.g. GEMINI_API_KEY missing: fall back to Stage 3.3 text
            logger.warning("Gemini composer unavailable: %s", getattr(exc, "safe_message", type(exc).__name__))
            return CompositionResult("failed", error="Gemini is not configured (GEMINI_API_KEY).")
        if composer is None:
            return CompositionResult("failed", error="Gemini is not configured (GEMINI_API_KEY).")
        inp = CompositionInput(label, week_numbers(window), stories, context)

        async def _run():
            try:
                return await composer.compose(inp)
            finally:
                await composer.provider.aclose()

        return asyncio.run(_run())

    def _defer_for_quota(self, window, edition_id, job_id, actor, composition: CompositionResult):
        from app.intelligence.quota import next_reset

        now = self.clock()
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            edition.status = transition(edition.status, S.GENERATION_FAILED)
            edition.generation_attempts = max(0, edition.generation_attempts - 1)  # waiting is not an attempt
            edition.next_attempt_at = next_reset(now)
            edition.last_error = composition.error or "The Gemini free quota is used up."
            edition.generation = {**(edition.generation or {}), "pendingReason": "quota", "aiError": edition.last_error}
            self._audit(
                s,
                edition.id,
                A.GENERATION_DEFERRED,
                actor,
                details={"reason": "quota", "retryAfter": edition.next_attempt_at.isoformat()},
            )
        return GenerationReport(
            window.start_date,
            "quota_limited",
            f"Gemini quota used up; generation resumes after {edition.next_attempt_at.isoformat()}.",
            edition_id,
            S.GENERATION_FAILED.value,
        )

    def _record_failure(self, window, job_id, actor, message: str, edition_id: uuid.UUID | None = None):
        now = self.clock()
        gen = self.config.generation
        with self.db.write_session() as s:
            edition = s.scalar(
                select(Edition)
                .where(Edition.week_start == window.start_date, Edition.is_demo.is_(False))
                .with_for_update()
            )
            if edition is None or edition.status != S.GENERATING or edition.generation_job_id != job_id:
                return GenerationReport(window.start_date, "failed", message, edition.id if edition else None)
            edition.status = transition(edition.status, S.GENERATION_FAILED)
            delay = gen.retry_backoff_minutes * 2 ** max(0, edition.generation_attempts - 1)
            edition.next_attempt_at = now + timedelta(minutes=min(delay, 7 * 24 * 60))
            edition.last_error = message[:500]
            self._audit(
                s,
                edition.id,
                A.GENERATION_FAILED,
                actor,
                details={"error": message[:500], "attempt": edition.generation_attempts},
            )
            return GenerationReport(window.start_date, "failed", message, edition.id, edition.status)

    # ------------------------------------------------------------------ admin reads
    def list_editions(self, limit: int = 50) -> list[dict]:
        with self.db.write_session() as s:
            rows = s.scalars(
                select(Edition).where(Edition.is_demo.is_(False)).order_by(Edition.week_start.desc()).limit(limit)
            ).all()
            counts = dict(
                s.execute(
                    select(EditionItem.edition_id, func.count())
                    .where(EditionItem.edition_id.in_([r.id for r in rows]))
                    .group_by(EditionItem.edition_id)
                ).all()
            )
            return [self._summary(r, counts.get(r.id, 0)) for r in rows]

    @staticmethod
    def _summary(e: Edition, story_count: int) -> dict:
        return {
            "id": str(e.id),
            "slug": e.slug,
            "number": e.number,
            "title": e.title,
            "weekStart": e.week_start.isoformat(),
            "weekEnd": e.week_end.isoformat(),
            "timezone": e.timezone,
            "status": e.status,
            "currentRevision": e.current_revision,
            "storyCount": story_count,
            "generatedAt": e.generated_at.isoformat() if e.generated_at else None,
            "approvedAt": e.approved_at.isoformat() if e.approved_at else None,
            "publishedAt": e.published_at.isoformat() if e.published_at else None,
            "generationAttempts": e.generation_attempts,
            "nextAttemptAt": e.next_attempt_at.isoformat() if e.next_attempt_at else None,
            "lastError": e.last_error,
        }

    def detail(self, edition_id: uuid.UUID, *, actor: Actor | None = None) -> dict:
        with self.db.write_session() as s:
            edition = s.scalar(select(Edition).where(Edition.id == edition_id, Edition.is_demo.is_(False)))
            if edition is None:
                raise errors.not_found()
            rev = self._current_revision(s, edition)
            content = self._content(rev)
            issues = (self._validate(content) + self._db_issues(s, edition, content)) if content else []
            admins = {a.user_id: a for a in s.scalars(select(EditorialAdmin)).all()}

            def who(uid):
                a = admins.get(uid)
                return {"id": str(uid), "email": a.email, "name": a.display_name} if a else None

            revisions = s.scalars(
                select(EditionRevision)
                .where(EditionRevision.edition_id == edition.id)
                .order_by(EditionRevision.revision.desc())
            ).all()
            audit = s.scalars(
                select(EditionAuditEntry)
                .where(EditionAuditEntry.edition_id == edition.id)
                .order_by(EditionAuditEntry.id.desc())
                .limit(200)
            ).all()
            if actor is not None and actor.user_id and edition.status in (S.REVIEW_REQUIRED, S.APPROVED):
                # Serialise simultaneous opens by the same admin so "opened for review" is logged once per revision.
                s.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                    {"k": f"edition-open:{edition.id}:{actor.user_id}"},
                )
                seen = s.scalar(
                    select(EditionAuditEntry.id)
                    .where(
                        EditionAuditEntry.edition_id == edition.id,
                        EditionAuditEntry.action == A.OPENED_FOR_REVIEW,
                        EditionAuditEntry.actor_id == actor.user_id,
                        EditionAuditEntry.revision == edition.current_revision,
                    )
                    .limit(1)
                )
                if seen is None:
                    self._audit(s, edition.id, A.OPENED_FOR_REVIEW, actor, revision=edition.current_revision)
            items = {
                i.cluster_id: i
                for i in s.scalars(select(EditionItem).where(EditionItem.edition_id == edition.id)).all()
            }
            data = self._summary(edition, len(items))
            data.update(
                {
                    "introduction": edition.introduction,
                    "editorialNote": edition.editorial_note,
                    "content": content.model_dump(mode="json", by_alias=False) if content else None,
                    "contentHash": rev.content_hash if rev else None,
                    "issues": _issue_dicts(issues),
                    "allowedActions": allowed_actions(edition.status, has_issues=has_errors(issues)),
                    "generation": edition.generation,
                    "rankingRunId": str(edition.ranking_run_id) if edition.ranking_run_id else None,
                    "approval": {
                        "revision": edition.approved_revision,
                        "contentHash": edition.approved_content_hash,
                        "by": who(edition.approved_by) if edition.approved_by else None,
                        "at": edition.approved_at.isoformat() if edition.approved_at else None,
                    },
                    "publication": {
                        "revision": edition.published_revision,
                        "by": who(edition.published_by) if edition.published_by else None,
                        "at": edition.published_at.isoformat() if edition.published_at else None,
                    },
                    "reviewNote": edition.review_note,
                    "publishedStories": [
                        {"clusterId": str(cid), "storyId": str(i.story_id)} for cid, i in items.items() if i.story_id
                    ],
                    "revisions": [
                        {
                            "revision": r.revision,
                            "origin": r.origin,
                            "contentHash": r.content_hash,
                            "createdAt": r.created_at.isoformat() if r.created_at else None,
                            "createdBy": who(r.created_by) if r.created_by else None,
                            "changes": r.changes,
                            "issueCount": len(r.issues or []),
                        }
                        for r in revisions
                    ],
                    "audit": [
                        {
                            "action": a.action,
                            "revision": a.revision,
                            "actorKind": a.actor_kind,
                            "actor": who(a.actor_id) if a.actor_id else None,
                            "at": a.occurred_at.isoformat() if a.occurred_at else None,
                            "details": a.details,
                        }
                        for a in audit
                    ],
                }
            )
            return data

    def revision_content(self, edition_id: uuid.UUID, revision: int) -> dict:
        with self.db.write_session() as s:
            rev = s.scalar(
                select(EditionRevision)
                .join(Edition, Edition.id == EditionRevision.edition_id)
                .where(
                    EditionRevision.edition_id == edition_id,
                    EditionRevision.revision == revision,
                    Edition.is_demo.is_(False),
                )
            )
            if rev is None:
                raise errors.not_found()
            return {
                "revision": rev.revision,
                "origin": rev.origin,
                "contentHash": rev.content_hash,
                "content": rev.content,
            }

    def candidates(self, edition_id: uuid.UUID) -> list[dict]:
        """Stories of the edition's ranking run an admin may add (eligible or review_required)."""
        with self.db.write_session() as s:
            edition = s.scalar(select(Edition).where(Edition.id == edition_id, Edition.is_demo.is_(False)))
            if edition is None:
                raise errors.not_found()
            if edition.ranking_run_id is None:
                return []
            _, ranking = self._ranking(s, run_id=edition.ranking_run_id)
            if ranking is None:
                return []
            in_edition = set(s.scalars(select(EditionItem.cluster_id).where(EditionItem.edition_id == edition.id)))
            cands = addable_candidates(ranking, in_edition)
            analyses = self._analyses(s, [c.story_cluster_id for c in cands])
        out = []
        for c in cands:
            analysis = analyses.get(c.story_cluster_id)
            if analysis is None or analysis.cluster_revision != c.cluster_revision:
                continue
            story = build_story(
                analysis,
                c,
                source="admin_added",
                eligibility=c.eligibility_status,
                ranking_run_id=edition.ranking_run_id,
            )
            if story is None:
                continue
            out.append(
                {
                    "clusterId": str(c.story_cluster_id),
                    "eligibility": c.eligibility_status,
                    "rank": c.rank,
                    "finalPriorityScore": c.final_scores.final_priority_score,
                    "priority": c.priority_classification,
                    "reviewFlags": list(c.review_flags),
                    "selectionNote": c.selection_note,
                    "category": story.category,
                    "publisher": story.publisher,
                    "sourceUrl": story.source_url,
                    "publishedDate": story.published_date.isoformat() if story.published_date else None,
                    "text": story.text.model_dump(),
                }
            )
        return out

    def preview(self, edition_id: uuid.UUID, revision: int | None = None) -> dict:
        """The public shape (as /editions/{slug} would return it) of the current revision, not published."""
        with self.db.write_session() as s:
            edition = s.scalar(select(Edition).where(Edition.id == edition_id, Edition.is_demo.is_(False)))
            if edition is None:
                raise errors.not_found()
            rev = self._current_revision(s, edition)
            if revision is not None and (rev is None or rev.revision != revision):
                raise errors.stale(edition.current_revision)
            content = self._content(rev)
        if content is None:
            raise errors.invalid_state("This edition has no content yet.")
        stories = []
        for pos, st in enumerate(content.stories, start=1):
            f = public_story_fields(st, edition_slug=edition.slug, position=pos)
            stories.append(
                {
                    "slug": f["slug"],
                    "provenance": "live",
                    "editionId": edition.slug,
                    "headline": f["title"],
                    "dek": f["summary"],
                    "body": [p for p in f["content"].split("\n\n") if p],
                    "topic": f["topic"],
                    "sourceName": st.publisher,
                    "sourceUrl": f["original_url"],
                    "publishedAt": (st.published_date or content.week_end).isoformat(),
                    "readingMinutes": f["reading_time_minutes"],
                    "impact": {
                        "whyItMatters": f["impact_why_it_matters"],
                        "explore": f["impact_explore"],
                        "buildIdea": f["impact_build_idea"] or "",
                        "disciplines": f["impact_disciplines"],
                    },
                    "image": None,
                    "featured": f["is_featured"],
                    "tags": f["tags"],
                }
            )
        return {
            "id": edition.slug,
            "slug": edition.slug,
            "number": edition.number,
            "title": content.title,
            "weekOf": content.week_start.isoformat(),
            "weekEnd": content.week_end.isoformat(),
            "introduction": content.introduction,
            "editorialNote": content.editorial_note or None,
            "provenance": "live",
            "stories": stories,
            "preview": True,
            "revision": rev.revision,
        }

    # ------------------------------------------------------------------ admin writes
    def save_revision(self, edition_id: uuid.UUID, actor: Actor, data: RevisionInput) -> dict:
        lim = self.config.limits
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            self._check_expected(edition, data.expected_revision)
            if S(edition.status) not in EDITABLE:
                raise errors.invalid_state(f"An edition in status '{edition.status}' cannot be edited.")
            prev_rev = self._current_revision(s, edition)
            prev = self._content(prev_rev)
            if prev is None:
                raise errors.invalid_state("This edition has no content to revise yet.")
            if len(data.stories) > 20:
                raise errors.EditionError("At most 20 stories per edition.", status_code=422, code="too_many_stories")
            ids = [st.cluster_id for st in data.stories]
            if len(ids) != len(set(ids)):
                raise errors.EditionError("A story is listed twice.", status_code=422, code="duplicate_story")
            existing = {st.cluster_id: st for st in prev.stories}
            new_ids = [cid for cid in ids if cid not in existing]
            added: dict[uuid.UUID, EditionStory] = {}
            if new_ids:
                added = self._new_stories(s, edition, new_ids, actor, data.acknowledge_review)
            stories = []
            for st in data.stories:
                base = existing.get(st.cluster_id) or added[st.cluster_id]
                new_text = StoryText(
                    headline=clean_text(st.text.headline, multiline=False)[: lim.headline * 2],
                    summary=clean_text(st.text.summary)[: lim.summary * 2],
                    technical_explanation=clean_text(st.text.technical_explanation)[: lim.technical_explanation * 2],
                    why_it_matters=clean_text(st.text.why_it_matters)[: lim.why_it_matters * 2],
                    student_relevance=clean_text(st.text.student_relevance)[: lim.student_relevance * 2],
                    project_idea=clean_text(st.text.project_idea)[: lim.project_idea * 2],
                )
                stories.append(base.model_copy(update={"text": new_text}))
            content = prev.model_copy(
                update={
                    "title": clean_text(data.title, multiline=False)[: lim.title * 2],
                    "introduction": clean_text(data.introduction)[: lim.introduction * 2],
                    "editorial_note": clean_text(data.editorial_note)[: lim.editorial_note * 2],
                    "stories": stories,
                }
            )
            if content_hash(content) == prev_rev.content_hash:
                return {"changed": False, "revision": edition.current_revision, "status": edition.status}
            changes = diff_contents(prev, content)
            issues = self._validate(content) + self._db_issues(s, edition, content)
            was_approved = edition.status == S.APPROVED
            rev = self._write_revision(
                s, edition, content, origin="admin_edit", actor=actor, changes=changes, issues=issues
            )
            for cid in changes["added"]:
                self._audit(s, edition.id, A.STORY_ADDED, actor, revision=rev.revision, details={"clusterId": cid})
            for cid in changes["removed"]:
                self._audit(s, edition.id, A.STORY_REMOVED, actor, revision=rev.revision, details={"clusterId": cid})
            if changes["reordered"]:
                self._audit(
                    s,
                    edition.id,
                    A.STORY_REORDERED,
                    actor,
                    revision=rev.revision,
                    details={"before": changes["orderBefore"], "after": changes["orderAfter"]},
                )
            if changes["editionFields"] or changes["edited"]:
                self._audit(
                    s,
                    edition.id,
                    A.CONTENT_EDITED,
                    actor,
                    revision=rev.revision,
                    details={"editionFields": changes["editionFields"], "stories": changes["edited"]},
                )
            new_status = after_edit(edition.status)
            if was_approved:
                self._audit(
                    s,
                    edition.id,
                    A.APPROVAL_INVALIDATED,
                    actor,
                    revision=rev.revision,
                    details={"approvedRevision": edition.approved_revision, "reason": "content changed"},
                )
                edition.approved_revision = None
                edition.approved_content_hash = None
                edition.approved_by = None
                edition.approved_at = None
            if new_status != edition.status:
                edition.status = transition(edition.status, new_status)
            if edition.status == S.REVIEW_REQUIRED and has_errors(issues):
                edition.status = transition(edition.status, S.DRAFT)
            return {
                "changed": True,
                "revision": rev.revision,
                "status": edition.status,
                "contentHash": rev.content_hash,
                "issues": _issue_dicts(issues),
            }

    def _new_stories(self, s, edition, cluster_ids, actor, acknowledged) -> dict[uuid.UUID, EditionStory]:
        if edition.ranking_run_id is None:
            raise errors.EditionError(
                "This edition has no Stage 3.4 ranking to add stories from.", status_code=422, code="no_ranking"
            )
        _, ranking = self._ranking(s, run_id=edition.ranking_run_id)
        if ranking is None:
            raise errors.EditionError("The edition's ranking run is gone.", status_code=422, code="no_ranking")
        cands: dict[uuid.UUID, RankedCandidate] = {c.story_cluster_id: c for c in addable_candidates(ranking, set())}
        analyses = self._analyses(s, cluster_ids)
        out = {}
        for cid in cluster_ids:
            cand = cands.get(cid)
            if cand is None:
                raise errors.EditionError(
                    "Only eligible or review-required candidates of this edition's Stage 3.4 ranking can be added.",
                    status_code=422,
                    code="not_a_candidate",
                )
            if cand.eligibility_status == "review_required" and cid not in acknowledged:
                raise errors.EditionError(
                    "This story needs review (Stage 3.4 flagged it). Confirm you reviewed it to add it.",
                    status_code=422,
                    code="review_not_acknowledged",
                )
            analysis = analyses.get(cid)
            if analysis is None or analysis.cluster_revision != cand.cluster_revision:
                raise errors.EditionError(
                    "This story's Stage 3.3 record is missing or changed since ranking.",
                    status_code=422,
                    code="source_record_missing",
                )
            story = build_story(
                analysis,
                cand,
                source="admin_added",
                eligibility=cand.eligibility_status,
                ranking_run_id=edition.ranking_run_id,
                acknowledged_by=actor.user_id if cand.eligibility_status == "review_required" else None,
            )
            if story is None:
                raise errors.EditionError("This story has no source with a valid URL.", status_code=422, code="no_url")
            out[cid] = story
        return out

    def submit(self, edition_id: uuid.UUID, actor: Actor, expected_revision: int) -> dict:
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            self._check_expected(edition, expected_revision)
            if edition.status != S.DRAFT:
                raise errors.invalid_state(f"Only drafts can be submitted for review (status is {edition.status}).")
            content = self._content(self._current_revision(s, edition))
            issues = (self._validate(content) + self._db_issues(s, edition, content)) if content else []
            if content is None or has_errors(issues):
                raise errors.validation_failed([i.model_dump() for i in issues if i.severity == "error"])
            edition.status = transition(edition.status, S.REVIEW_REQUIRED)
            self._audit(s, edition.id, A.APPROVAL_REQUESTED, actor, revision=edition.current_revision)
            return {"status": edition.status, "revision": edition.current_revision}

    def approve(self, edition_id: uuid.UUID, actor: Actor, expected_revision: int, reviewed_hash: str) -> dict:
        if actor.kind != "admin" or actor.user_id is None:
            raise errors.EditionError("Only an authorised admin can approve.", status_code=403, code="forbidden")
        refused: list[Issue] = []
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            self._check_expected(edition, expected_revision)
            rev = self._current_revision(s, edition)
            if (
                edition.status == S.APPROVED
                and edition.approved_revision == expected_revision
                and rev
                and (edition.approved_content_hash == reviewed_hash == rev.content_hash)
            ):
                return {"status": edition.status, "revision": edition.current_revision, "alreadyApproved": True}
            if edition.status != S.REVIEW_REQUIRED:
                raise errors.invalid_state(
                    f"Only editions awaiting review can be approved (status is {edition.status})."
                )
            if rev is None or rev.content_hash != reviewed_hash:
                raise errors.stale(edition.current_revision)
            content = self._content(rev)
            issues = self._validate(content) + self._db_issues(s, edition, content)
            refused = [i for i in issues if i.severity == "error"]
            if refused:
                self._audit(
                    s,
                    edition.id,
                    A.APPROVAL_REJECTED,
                    actor,
                    revision=rev.revision,
                    details={"issues": [i.code for i in refused][:20]},
                )
            else:
                edition.status = transition(edition.status, S.APPROVED)
                edition.approved_revision = rev.revision
                edition.approved_content_hash = rev.content_hash
                edition.approved_by = actor.user_id
                edition.approved_at = self.clock()
                self._audit(
                    s, edition.id, A.APPROVED, actor, revision=rev.revision, details={"contentHash": rev.content_hash}
                )
                result = {
                    "status": edition.status,
                    "revision": rev.revision,
                    "approvedAt": edition.approved_at.isoformat(),
                }
        if refused:
            raise errors.validation_failed([i.model_dump() for i in refused])
        return result

    def reject(self, edition_id: uuid.UUID, actor: Actor, expected_revision: int, reason: str, *, to_draft: bool):
        reason = clean_text(reason)[: self.config.limits.review_note]
        if len(reason) < 3:
            raise errors.EditionError("Give a short reason.", status_code=422, code="reason_required")
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            self._check_expected(edition, expected_revision)
            if edition.status != S.REVIEW_REQUIRED:
                raise errors.invalid_state(
                    f"Only editions awaiting review can be rejected (status is {edition.status})."
                )
            edition.status = transition(edition.status, S.DRAFT if to_draft else S.REJECTED)
            edition.review_note = reason
            self._audit(
                s,
                edition.id,
                A.RETURNED_FOR_REVISION if to_draft else A.REJECTED,
                actor,
                revision=edition.current_revision,
                details={"reason": reason},
            )
            return {"status": edition.status, "revision": edition.current_revision}

    def archive(self, edition_id: uuid.UUID, actor: Actor, expected_revision: int) -> dict:
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            self._check_expected(edition, expected_revision)
            edition.status = transition(edition.status, S.ARCHIVED)
            self._audit(s, edition.id, A.ARCHIVED, actor, revision=edition.current_revision)
            return {"status": edition.status, "revision": edition.current_revision}

    def regenerate(self, edition_id: uuid.UUID, actor: Actor, expected_revision: int) -> GenerationReport:
        with self.db.write_session() as s:
            edition = self._lock(s, edition_id)
            self._check_expected(edition, expected_revision)
            if S(edition.status) not in REGENERATABLE:
                raise errors.invalid_state(f"An edition in status '{edition.status}' cannot be regenerated.")
            week_start = edition.week_start
        return self.generate_week(week_start, actor=actor, force=True)

    def publish(self, edition_id: uuid.UUID, actor: Actor, expected_revision: int) -> dict:
        if actor.kind != "admin" or actor.user_id is None:
            raise errors.EditionError("Only an authorised admin can publish.", status_code=403, code="forbidden")
        try:
            with self.db.write_session() as s:
                edition = self._lock(s, edition_id)
                if edition.status == S.PUBLISHED:
                    if edition.published_revision == expected_revision:
                        return {
                            "status": edition.status,
                            "revision": edition.published_revision,
                            "slug": edition.slug,
                            "alreadyPublished": True,
                        }
                    raise errors.invalid_state("This edition is already published from another revision.")
                self._check_expected(edition, expected_revision)
                if edition.status != S.APPROVED:
                    raise errors.invalid_state(f"Only approved editions can be published (status is {edition.status}).")
                rev = self._current_revision(s, edition)
                if (
                    rev is None
                    or edition.approved_revision != rev.revision
                    or edition.approved_content_hash != rev.content_hash
                ):
                    raise errors.invalid_state("The approval does not match the current revision; approve it again.")
                content = self._content(rev)
                issues = self._validate(content) + self._db_issues(s, edition, content)
                if has_errors(issues):
                    raise errors.validation_failed([i.model_dump() for i in issues if i.severity == "error"])
                now = self.clock()
                self._materialise(s, edition, content, now)
                edition.status = transition(edition.status, S.PUBLISHED)
                edition.published_at = now
                edition.published_revision = rev.revision
                edition.published_by = actor.user_id
                self._audit(
                    s,
                    edition.id,
                    A.PUBLISHED,
                    actor,
                    revision=rev.revision,
                    details={"contentHash": rev.content_hash, "stories": len(content.stories)},
                )
                return {"status": edition.status, "revision": rev.revision, "slug": edition.slug}
        except errors.EditionError as exc:
            if exc.code in ("edition_invalid",):
                self._audit_failure(edition_id, actor, expected_revision, exc.code)
            raise
        except Exception as exc:  # noqa: BLE001 - nothing was published (rolled back); record and report safely
            logger.exception("publishing edition %s failed", edition_id)
            self._audit_failure(edition_id, actor, expected_revision, type(exc).__name__)
            raise errors.EditionError(
                "Publishing failed and nothing was published. The edition stays approved; try again.",
                status_code=500,
                code="publish_failed",
            ) from None

    def _audit_failure(self, edition_id, actor, revision, reason):
        try:
            with self.db.write_session() as s:
                if s.scalar(select(Edition.id).where(Edition.id == edition_id)):
                    self._audit(s, edition_id, A.PUBLISH_FAILED, actor, revision=revision, details={"reason": reason})
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("could not record the publish failure")

    def _materialise(self, s: Session, edition: Edition, content: EditionContent, now: datetime) -> None:
        """Create the public `stories` rows (and any missing `sources`) from the approved snapshot."""
        for pos, st in enumerate(content.stories, start=1):
            f = public_story_fields(st, edition_slug=edition.slug, position=pos)
            source = self._source_for(s, st)
            published_date = f.pop("original_published_at_date")
            story = Story(
                slug=f["slug"],
                title=f["title"][:300],
                summary=f["summary"],
                content=f["content"],
                topic=f["topic"],
                tags=f["tags"],
                status="published",
                is_demo=False,
                is_featured=f["is_featured"],
                edition_id=edition.id,
                edition_position=pos,
                source_id=source.id,
                original_url=f["original_url"],
                canonical_url=canonicalize_url(f["original_url"]),
                original_published_at=(
                    datetime.combine(published_date, time(0, 0), tzinfo=UTC) if published_date else None
                ),
                published_at=now,
                reading_time_minutes=f["reading_time_minutes"],
                impact_why_it_matters=f["impact_why_it_matters"],
                impact_explore=f["impact_explore"],
                impact_build_idea=f["impact_build_idea"],
                impact_disciplines=f["impact_disciplines"],
            )
            s.add(story)
            s.flush()
            item = s.scalar(
                select(EditionItem).where(EditionItem.edition_id == edition.id, EditionItem.cluster_id == st.cluster_id)
            )
            if item is not None:
                item.story_id = story.id

    @staticmethod
    def _source_for(s: Session, st: EditionStory) -> Source:
        host = (urlsplit(st.source_url).hostname or "").lower().rstrip(".").removeprefix("www.")
        kind = SOURCE_KIND_MAP.get(st.source_kind or "", "news_site")
        source = s.scalar(select(Source).where(Source.canonical_domain == host, Source.kind == kind))
        if source is not None:
            return source
        from app.editions.content import slugify

        base = slugify(f"{host}-{kind}", 110)
        slug, n = base, 1
        while s.scalar(select(Source.id).where(Source.slug == slug)):
            n += 1
            slug = f"{base}-{n}"
        source = Source(
            slug=slug, name=st.publisher[:200], kind=kind, canonical_domain=host, homepage_url=f"https://{host}/"
        )
        try:
            with s.begin_nested():
                s.add(source)
                s.flush()
        except IntegrityError:
            source = s.scalar(select(Source).where(Source.canonical_domain == host, Source.kind == kind))
        return source


def diff_contents(before: EditionContent, after: EditionContent) -> dict:
    b_ids = [str(s.cluster_id) for s in before.stories]
    a_ids = [str(s.cluster_id) for s in after.stories]
    common_b = [i for i in b_ids if i in a_ids]
    common_a = [i for i in a_ids if i in b_ids]
    b_map = {str(s.cluster_id): s for s in before.stories}
    edited = []
    for s in after.stories:
        old = b_map.get(str(s.cluster_id))
        if old is None:
            continue
        fields = [f for f in EDITORIAL_FIELDS if getattr(old.text, f) != getattr(s.text, f)]
        if fields:
            edited.append({"clusterId": str(s.cluster_id), "fields": fields})
    return {
        "added": [i for i in a_ids if i not in b_ids],
        "removed": [i for i in b_ids if i not in a_ids],
        "reordered": common_b != common_a,
        "orderBefore": b_ids,
        "orderAfter": a_ids,
        "edited": edited,
        "editionFields": [
            f for f in ("title", "introduction", "editorial_note") if getattr(before, f) != getattr(after, f)
        ],
    }
