"""Helpers for Stage 3.6 tests: a real published Stage 3.5 edition (generated, approved and published through the
Stage 3.5 service against the rolled-back test database) plus extra, unselected working data of the same week.

All destructive tests run inside the test transaction that is rolled back; nothing ever touches Supabase."""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cleanup.config import RetentionConfig
from app.cleanup.service import CleanupService
from app.editions.service import Actor
from app.models import (
    CleanupJob,
    EditionItem,
    IngestionItem,
    RankingRun,
    StoryAnalysisRecord,
    StoryEmbedding,
    StoryVerificationRecord,
)
from app.ranking.window import EditionWindow
from tests.conftest import make_settings
from tests.edition_helpers import (
    ADMIN,
    WEEK_START,
    SavepointDatabase,
    edition_config,
    make_service,
    pipeline,
    seed_admins,
    seed_pipeline,
)

WINDOW = EditionWindow(WEEK_START, "Asia/Kolkata")
# Late enough for every delay of the default policy (week end + 35 days), and always well after the rows' real
# created_at/updated_at (the database's now()), so "recently active" never applies by accident.
LATER = max(datetime(2026, 12, 20, 6, 0, tzinfo=UTC), datetime.now(UTC) + timedelta(days=5))
AFTER_HOLD = LATER + timedelta(hours=25)
SCHEDULER = Actor.scheduler()
OLD = datetime(2026, 10, 1, tzinfo=UTC)


def retention(**sections) -> RetentionConfig:
    base = RetentionConfig().model_dump()
    for name, values in sections.items():
        if isinstance(values, dict):
            base[name].update(values)
        else:
            base[name] = values
    return RetentionConfig.model_validate(base)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_cleanup(session: Session, *, clock: Clock | None = None, config: RetentionConfig | None = None, **cfg):
    settings = make_settings()
    clock = clock or Clock(LATER)
    service = CleanupService(
        settings,
        SavepointDatabase(settings, session),
        config or retention(**cfg),
        edition_config(),
        clock=clock,
    )
    service.test_clock = clock
    return service


def vector(seed: int) -> list[float]:
    return [((seed * 7 + i) % 13) / 13.0 for i in range(768)]


def add_working_data(
    session: Session,
    cluster_id: uuid.UUID,
    *,
    week: date = WEEK_START,
    analysis: bool = True,
    verification: bool = True,
    embedding: bool = True,
    expires_at: datetime | None = None,
    touched_at: datetime | None = None,
    status: str = "completed",
) -> None:
    """Stage 3.3/3.4 working data of one (unselected) story. `touched_at` sets created_at/updated_at explicitly."""
    rows, _ = pipeline()
    template = rows[0]
    stamps = {"created_at": touched_at, "updated_at": touched_at} if touched_at else {}
    expires = expires_at or datetime(2026, 12, 1, tzinfo=UTC)
    if analysis:
        session.add(
            StoryAnalysisRecord(
                **{
                    **template,
                    "cluster_id": cluster_id,
                    "status": status,
                    "analysis": template["analysis"] if status in ("completed", "needs_review") else None,
                    "expires_at": expires,
                    "job_id": None,
                },
                **stamps,
            )
        )
        session.flush()
    if verification:
        session.add(
            StoryVerificationRecord(
                cluster_id=cluster_id,
                cluster_revision="r1",
                input_key="k" * 64,
                ranking_config_version="cfg",
                verification_config_version="vcfg",
                edition_start=week,
                verification_status="completed",
                eligibility="ineligible",
                final_priority_score=40,
                evaluated_at=datetime(2026, 11, 8, tzinfo=UTC),
                expires_at=expires,
                **stamps,
            )
        )
    if embedding and analysis:
        session.add(
            StoryEmbedding(
                cluster_id=cluster_id,
                cluster_revision="r1",
                embedding=vector(hash(cluster_id) % 97),
                model="gemini-embedding-2",
                dimensions=768,
                embedding_version="v1",
                content_hash="h" * 64,
                input_chars=100,
                first_collected_at=WINDOW.start + timedelta(hours=3),
                last_collected_at=WINDOW.start + timedelta(hours=5),
                embedded_at=WINDOW.start + timedelta(hours=6),
                **stamps,
            )
        )
    session.flush()


def add_run(session: Session, *, week: date = WEEK_START, status: str = "incomplete", hours_after: int = -48,
            candidates: list[uuid.UUID] = ()) -> RankingRun:  # fmt: skip
    _, result = pipeline()
    data = {**result, "candidates": [{**result["candidates"][0], "storyClusterId": str(c)} for c in candidates]}
    run = RankingRun(
        run_key=uuid.uuid4().hex,
        edition_start=week,
        edition_end=week + timedelta(days=6),
        timezone="Asia/Kolkata",
        status=status,
        ranking_config_version=result["rankingConfigVersion"],
        verification_config_version=result["verificationConfigVersion"],
        processed_at=EditionWindow(week, "Asia/Kolkata").end + timedelta(hours=hours_after),
        result=data,
    )
    session.add(run)
    session.flush()
    return run


def add_staged_item(session: Session, cluster_id: uuid.UUID) -> None:
    n = uuid.uuid4().hex
    session.add(
        IngestionItem(
            source_key="test-feed",
            source_type="rss",
            dedup_key=n,
            title="A staged item still in the pipeline",
            original_url=f"https://example.org/{n}",
            canonical_url=f"https://example.org/{n}",
            feed_identifier=n,
            collected_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=14),
            cluster_id=cluster_id,
        )
    )
    session.flush()


def published_edition(session: Session):
    """(edition id, selected cluster ids, the ranking run it was built from), published at MONDAY."""
    seed_admins(session)
    run = seed_pipeline(session)
    editions = make_service(session)
    report = editions.generate_week(WEEK_START, actor=SCHEDULER)
    eid = report.edition_id
    d = editions.detail(eid)
    editions.approve(eid, ADMIN, d["currentRevision"], d["contentHash"])
    editions.publish(eid, ADMIN, d["currentRevision"])
    selected = list(session.scalars(select(EditionItem.cluster_id).where(EditionItem.edition_id == eid)))
    return eid, selected, run, editions


def unselected_clusters(session: Session, selected: list[uuid.UUID]) -> list[uuid.UUID]:
    rows, _ = pipeline()
    return [r["cluster_id"] for r in rows if r["cluster_id"] not in selected]


def job_for(session: Session, edition_id) -> CleanupJob | None:
    session.flush()
    session.expire_all()
    return session.scalar(select(CleanupJob).where(CleanupJob.edition_id == edition_id))


def exists(session: Session, model, cluster_id) -> bool:
    return session.scalar(select(model.id).where(model.cluster_id == cluster_id)) is not None
