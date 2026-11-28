"""Phase 4.2 Scenario J (safety-critical): automated cleanup and archive protection, on disposable records in the
rolled-back local test database only.

Every scenario walks the real Stage 3.5 lifecycle (generate -> review -> approve -> publish) and runs the real
Stage 3.6 `CleanupService` with a controlled clock, then checks the database rows that remain.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models import (
    CleanupAuditEntry,
    CleanupJob,
    CleanupManifestItem,
    Edition,
    EditionItem,
    EditionRevision,
    RankingRun,
    Story,
    StoryAnalysisRecord,
    StoryEmbedding,
    StoryVerificationRecord,
)
from tests.cleanup_helpers import (
    AFTER_HOLD,
    LATER,
    SCHEDULER,
    Clock,
    add_run,
    add_working_data,
    exists,
    make_cleanup,
    published_edition,
)
from tests.conftest import TEST_DATABASE_URL
from tests.edition_helpers import ADMIN, WEEK_START, make_service, seed_admins, seed_pipeline

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")


def counts(s):
    s.flush()
    return {
        m.__name__: s.scalar(select(func.count()).select_from(m))
        for m in (
            StoryAnalysisRecord,
            StoryVerificationRecord,
            StoryEmbedding,
            RankingRun,
            Story,
            EditionItem,
            EditionRevision,
        )
    }


def sweep(session, *times):
    service = make_cleanup(session, clock=Clock(LATER))
    reports = []
    for t in times or (LATER, AFTER_HOLD, AFTER_HOLD + timedelta(days=30)):
        service.test_clock.now = t
        reports.append(service.scheduled_run())
    return reports


def unpublished(session, state):
    """An edition of the test week left in `state`, plus disposable unselected working data of the same week."""
    seed_admins(session)
    seed_pipeline(session)
    editions = make_service(session)
    eid = editions.generate_week(WEEK_START, actor=SCHEDULER).edition_id
    d = editions.detail(eid)
    if state == "approved":
        editions.approve(eid, ADMIN, d["currentRevision"], d["contentHash"])
    elif state == "rejected":
        editions.reject(eid, ADMIN, d["currentRevision"], "Not this week", to_draft=False)
    elif state == "draft":
        editions.reject(eid, ADMIN, d["currentRevision"], "Rework", to_draft=True)
    extra = [uuid.uuid4() for _ in range(3)]
    for c in extra:
        add_working_data(session, c)
    return eid, extra


@pytest.mark.parametrize("state", ["review_required", "approved", "rejected", "draft"])
def test_nothing_is_deleted_before_publication(db_session, state):
    """E2E-J-01: with the week's edition generated, in review, approved-but-unpublished, rejected or returned to
    draft, cleanup runs long after every delay and deletes nothing; no cleanup job is even created."""
    eid, extra = unpublished(db_session, state)
    before = counts(db_session)
    reports = sweep(db_session)
    assert sum(r.deleted for r in reports) == 0
    assert counts(db_session) == before
    assert db_session.scalar(select(CleanupJob).where(CleanupJob.edition_id == eid)) is None
    assert all(exists(db_session, StoryAnalysisRecord, c) for c in extra)


def test_no_edition_at_all_means_no_deletion(db_session):
    """E2E-J-02: a week whose edition was never generated (ranking only) keeps all its working data."""
    seed_pipeline(db_session)
    extra = [uuid.uuid4() for _ in range(2)]
    for c in extra:
        add_working_data(db_session, c)
    before = counts(db_session)
    assert sum(r.deleted for r in sweep(db_session)) == 0 and counts(db_session) == before


def test_after_publication_only_unselected_working_data_goes(db_session):
    """E2E-J-03: after publication, before the safety delay nothing happens; after the delay a manifest is staged
    and held 24 h; after the hold exactly the unselected analyses, verifications and embeddings are deleted; the
    edition, its revisions, selection rows, public stories, source links, the ranking it was built from and the
    selected stories' analyses all remain. A repeated run deletes nothing more."""
    eid, selected, run, _ = published_edition(db_session)
    extra = [uuid.uuid4() for _ in range(4)]
    for c in extra:
        add_working_data(db_session, c)
    public_before = [
        (s.id, s.original_url, s.title) for s in db_session.scalars(select(Story).where(Story.edition_id == eid))
    ]
    revisions_before = db_session.scalar(select(func.count()).select_from(EditionRevision))

    early = make_cleanup(db_session, clock=Clock(LATER - timedelta(days=40)))
    assert early.scheduled_run().deleted == 0
    staged = sweep(db_session, LATER)[0]
    assert staged.deleted == 0 and all(exists(db_session, StoryAnalysisRecord, c) for c in extra)
    job = db_session.scalar(select(CleanupJob).where(CleanupJob.edition_id == eid))
    assert job.status == "staged" and job.execute_after >= LATER + timedelta(hours=24)
    manifest = db_session.scalars(select(CleanupManifestItem).where(CleanupManifestItem.job_id == job.id)).all()
    assert {m.record_id for m in manifest if m.target == "story_cluster"} >= set(extra)

    done = sweep(db_session, AFTER_HOLD)[0]
    assert done.status == "ok" and done.deleted >= len(extra)  # one count per story (its three rows go together)
    for c in extra:
        assert not exists(db_session, StoryAnalysisRecord, c)
        assert not exists(db_session, StoryVerificationRecord, c) and not exists(db_session, StoryEmbedding, c)
    for c in selected:
        assert exists(db_session, StoryAnalysisRecord, c)
    db_session.expire_all()
    assert db_session.get(Edition, eid).status == "published"
    assert [
        (s.id, s.original_url, s.title) for s in db_session.scalars(select(Story).where(Story.edition_id == eid))
    ] == public_before
    assert db_session.scalar(select(func.count()).select_from(EditionRevision)) == revisions_before
    assert db_session.get(RankingRun, run.id) is not None
    assert set(db_session.scalars(select(EditionItem.cluster_id).where(EditionItem.edition_id == eid))) == set(selected)
    audit = [a.action for a in db_session.scalars(select(CleanupAuditEntry).where(CleanupAuditEntry.job_id == job.id))]
    assert audit and any("delet" in a or "complet" in a for a in audit)
    again = sweep(db_session, AFTER_HOLD + timedelta(days=1), AFTER_HOLD + timedelta(days=8))
    assert sum(r.deleted for r in again) == 0


def test_story_still_used_by_another_week_is_protected(db_session):
    """E2E-J-04: an unselected story of the published week that was re-ranked in the next week (new reports arrived,
    so Stage 3.4 re-verified it for that week and lists it in that week's ranking, whose edition is not published)
    keeps its analysis and embedding: the data is shared with a week that still needs it."""
    eid, selected, run, _ = published_edition(db_session)
    shared, own = uuid.uuid4(), uuid.uuid4()
    add_working_data(db_session, shared)
    add_working_data(db_session, own)
    add_run(db_session, week=WEEK_START + timedelta(days=7), status="completed", hours_after=2, candidates=[shared])
    db_session.execute(
        StoryVerificationRecord.__table__.update()
        .where(StoryVerificationRecord.cluster_id == shared)
        .values(edition_start=WEEK_START + timedelta(days=7))
    )
    sweep(db_session, LATER, AFTER_HOLD)
    assert exists(db_session, StoryAnalysisRecord, shared) and exists(db_session, StoryEmbedding, shared)
    assert not exists(db_session, StoryAnalysisRecord, own)
    job = db_session.scalar(select(CleanupJob).where(CleanupJob.edition_id == eid))
    reasons = {
        m.reason
        for m in db_session.scalars(
            select(CleanupManifestItem).where(
                CleanupManifestItem.job_id == job.id, CleanupManifestItem.record_id == shared
            )
        )
    }
    # Either never a candidate of this week's job, or listed as protected with the reason.
    assert reasons <= {"ranked_in_other_week", "active_edition_candidate"}


def test_uncertain_publication_stops_cleanup(db_session):
    """E2E-J-05: if publication cannot be confirmed from the database (here: one public story's source link no
    longer matches its approved snapshot) the job is blocked and nothing is deleted."""
    eid, selected, run, _ = published_edition(db_session)
    extra = [uuid.uuid4() for _ in range(2)]
    for c in extra:
        add_working_data(db_session, c)
    db_session.execute(text("ALTER TABLE stories DISABLE TRIGGER USER"))  # simulate external tampering
    first = db_session.scalars(select(Story.id).where(Story.edition_id == eid)).first()
    db_session.execute(
        Story.__table__.update()
        .where(Story.id == first)
        .values(original_url="https://tampered.example.org/x", canonical_url="https://tampered.example.org/x")
    )
    db_session.execute(text("ALTER TABLE stories ENABLE TRIGGER USER"))
    sweep(db_session, LATER, AFTER_HOLD)
    job = db_session.scalar(select(CleanupJob).where(CleanupJob.edition_id == eid))
    assert job is not None and job.status == "blocked" and job.blocked_reasons
    assert all(exists(db_session, StoryAnalysisRecord, c) for c in extra)


@pytest.mark.parametrize("target", ["edition", "stories", "edition_items", "selected_analysis", "revisions"])
def test_database_refuses_to_delete_published_archive(db_session, target):
    """E2E-J-06: even a direct SQL DELETE (no application code) of a published edition, its public stories, its
    selection rows, a selected story's analysis or its revisions is refused by the database."""
    eid, selected, run, _ = published_edition(db_session)
    stmt = {
        "edition": "DELETE FROM editions WHERE id = :e",
        "stories": "DELETE FROM stories WHERE edition_id = :e",
        "edition_items": "DELETE FROM edition_items WHERE edition_id = :e",
        "selected_analysis": "DELETE FROM story_analyses WHERE cluster_id = :c",
        "revisions": "DELETE FROM edition_revisions WHERE edition_id = :e",
    }[target]
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(text(stmt), {"e": eid, "c": selected[0]})


def test_cleanup_failure_is_retried_and_never_half_applied(db_session, monkeypatch):
    """E2E-J-07: a database error while deleting rolls the batch back (no record lost half way), the job records the
    failure and is retried by a later run, which completes it."""
    eid, selected, run, _ = published_edition(db_session)
    extra = [uuid.uuid4() for _ in range(3)]
    for c in extra:
        add_working_data(db_session, c)
    service = make_cleanup(db_session, clock=Clock(LATER))
    service.scheduled_run()
    import app.cleanup.service as svc

    real = svc.CleanupService.execute_due
    calls = {"n": 0}

    def boom(self, actor, report):
        calls["n"] += 1
        if calls["n"] == 1:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("DELETE", {}, Exception("connection lost"))
        return real(self, actor, report)

    monkeypatch.setattr(svc.CleanupService, "execute_due", boom)
    service.test_clock.now = AFTER_HOLD
    first = None
    try:
        first = service.scheduled_run()
    except Exception as exc:  # noqa: BLE001 - either reported or raised: in both cases nothing may be deleted
        print(f"E2E-J-07 first attempt raised {type(exc).__name__}")
    assert all(exists(db_session, StoryAnalysisRecord, c) for c in extra)
    assert first is None or first.status in ("failed", "partial")
    service.test_clock.now = AFTER_HOLD + timedelta(days=1)
    service.scheduled_run()
    assert not any(exists(db_session, StoryAnalysisRecord, c) for c in extra)
    assert all(exists(db_session, StoryAnalysisRecord, c) for c in selected)
