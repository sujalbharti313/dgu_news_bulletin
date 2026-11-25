"""Stage 3.6 cleanup engine against PostgreSQL (rolled back): publication gate, protections, safety delays, manifest,
bounded batches, idempotency, retries, recovery and the public archive after cleanup. No network, no AI."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.cleanup import eligibility as el
from app.cleanup.service import CLEANUP_JOB_KEY, CleanupError
from app.editions.service import Actor
from app.models import (
    CleanupAuditEntry,
    CleanupJob,
    CleanupManifestItem,
    Edition,
    EditionItem,
    IngestionJob,
    RankingRun,
    Story,
    StoryAnalysisRecord,
    StoryEmbedding,
    StoryVerificationRecord,
)
from tests.cleanup_helpers import (
    AFTER_HOLD,
    LATER,
    OLD,
    SCHEDULER,
    WEEK_START,
    WINDOW,
    Clock,
    add_run,
    add_staged_item,
    add_working_data,
    exists,
    job_for,
    make_cleanup,
    published_edition,
    unselected_clusters,
)
from tests.edition_helpers import ADMIN, MONDAY

pytestmark = pytest.mark.usefixtures("engine")


@pytest.fixture
def week(db_session):
    """A published edition plus unselected working data (analysis + verification + embedding) of its week."""
    eid, selected, run, editions = published_edition(db_session)
    extra = [uuid.uuid4() for _ in range(4)]
    for c in extra:
        add_working_data(db_session, c)
    for c in selected:  # the selected stories have working data too: it must survive
        add_working_data(db_session, c, analysis=False, embedding=False)
    old_run = add_run(db_session, candidates=extra[:2])
    return db_session, eid, selected, extra, run.id, old_run.id


def actions(session, job_id):
    return [
        a.action
        for a in session.scalars(
            select(CleanupAuditEntry).where(CleanupAuditEntry.job_id == job_id).order_by(CleanupAuditEntry.id)
        )
    ]


def run_to_completion(service, session):
    service.test_clock.now = LATER
    service.scheduled_run()
    service.test_clock.now = AFTER_HOLD
    return service.scheduled_run()


# ------------------------------------------------------------------------------------------- publication gate
def test_published_week_is_cleaned_and_published_content_is_preserved(week, make_client):
    session, eid, selected, extra, run, old_run = week
    client = make_client()
    before = client.get("/api/v1/editions/current").json()
    service = make_cleanup(session)
    report = run_to_completion(service, session)
    assert report.status == "ok", report.as_dict()
    job = job_for(session, eid)
    assert job.status == "completed" and job.completed_at is not None
    for c in extra + unselected_clusters(session, selected):
        assert not exists(session, StoryAnalysisRecord, c)
        assert not exists(session, StoryVerificationRecord, c)
        assert not exists(session, StoryEmbedding, c)
    for c in selected:  # selected stories keep every working record
        assert exists(session, StoryAnalysisRecord, c) and exists(session, StoryVerificationRecord, c)
    assert session.get(RankingRun, run) is not None  # the edition's run
    assert session.get(RankingRun, old_run) is None  # superseded run
    after = client.get("/api/v1/editions/current").json()
    assert after == before  # the public edition is byte-for-byte the same
    assert [s["sourceUrl"] for s in after["stories"]] == [s["sourceUrl"] for s in before["stories"]]
    assert actions(session, job.id)[:3] == ["job_created", "publication_verified", "manifest_staged"]
    assert actions(session, job.id)[-1] == "deletion_completed"
    assert job.deleted_count == len(extra) + len(unselected_clusters(session, selected)) + 1


def test_unpublished_editions_never_get_a_cleanup_job(db_session):
    from tests.edition_helpers import make_service, seed_admins, seed_pipeline

    seed_admins(db_session)
    seed_pipeline(db_session)
    editions = make_service(db_session)
    eid = editions.generate_week(WEEK_START, actor=SCHEDULER).edition_id  # review_required
    extra = uuid.uuid4()
    add_working_data(db_session, extra)
    service = make_cleanup(db_session)
    for status in ("review_required", "approved"):
        if status == "approved":
            d = editions.detail(eid)
            editions.approve(eid, ADMIN, d["currentRevision"], d["contentHash"])
        run_to_completion(service, db_session)
        assert job_for(db_session, eid) is None
        assert exists(db_session, StoryAnalysisRecord, extra)
    with pytest.raises(CleanupError):
        service.plan_edition(eid, ADMIN)


def test_failed_publication_check_blocks_and_recovers(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    edition = session.get(Edition, eid)
    good_hash = edition.approved_content_hash
    session.execute(update(Edition).where(Edition.id == eid).values(approved_content_hash="0" * 64))
    run_to_completion(service, session)
    job = job_for(session, eid)
    assert job.status == "blocked" and "snapshot_hash_mismatch" in job.blocked_reasons
    assert all(exists(session, StoryAnalysisRecord, c) for c in extra)
    session.execute(update(Edition).where(Edition.id == eid).values(approved_content_hash=good_hash))
    run_to_completion(service, session)
    assert job_for(session, eid).status == "completed"


@pytest.mark.parametrize("breakage", ["source_link", "story_unpublished", "publish_failed_audit"])
def test_incomplete_or_uncertain_publication_blocks_deletion(week, breakage):
    session, eid, selected, extra, *_ = week
    story = session.scalars(select(Story).where(Story.edition_id == eid)).first()
    if breakage == "source_link":
        story.original_url, story.canonical_url = "https://example.org/other", "https://example.org/other"
    elif breakage == "story_unpublished":
        story.status = "draft"
    else:
        from app.models import EditionAuditEntry

        session.add(EditionAuditEntry(edition_id=eid, action="publish_failed", actor_kind="system"))
    session.flush()
    service = make_cleanup(session)
    run_to_completion(service, session)
    job = job_for(session, eid)
    assert job.status == "blocked", job.blocked_reasons
    assert all(exists(session, StoryAnalysisRecord, c) for c in extra)


def test_cleanup_is_not_started_before_the_safety_delay(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session, clock=Clock(MONDAY + timedelta(days=5)))
    service.scheduled_run()
    job = job_for(session, eid)
    assert job.status == "scheduled"
    # The later of published_at + 72 h and the week end + 35 days.
    assert job.not_before == WINDOW.end + timedelta(days=35)
    service.test_clock.now = job.not_before - timedelta(minutes=1)
    service.scheduled_run()
    assert job_for(session, eid).status == "scheduled"
    service.test_clock.now = job.not_before + timedelta(minutes=1)
    service.scheduled_run()
    job = job_for(session, eid)
    assert job.status == "staged"  # manifest saved, nothing deleted during the hold
    assert all(exists(session, StoryAnalysisRecord, c) for c in extra)
    service.test_clock.now = job.execute_after - timedelta(minutes=1)
    service.scheduled_run()
    assert job_for(session, eid).status == "staged"
    assert all(exists(session, StoryAnalysisRecord, c) for c in extra)


def test_configurable_delays(week):
    session, eid, *_ = week
    service = make_cleanup(
        session,
        clock=Clock(MONDAY),
        published_week={"safety_delay_hours": 24 * 60, "min_story_age_days": 40, "manifest_hold_hours": 2},
    )
    service.scheduled_run()
    assert job_for(session, eid).not_before == MONDAY + timedelta(days=60)


def test_disabled_policy_plans_and_deletes_nothing(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session, enabled=False)
    report = run_to_completion(service, session)
    assert report.status == "disabled" and job_for(session, eid) is None


# ------------------------------------------------------------------------------------------- protections
def test_manifest_lists_protected_records_with_reasons(week):
    session, eid, selected, extra, run, old_run = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    job = job_for(session, eid)
    items = session.scalars(select(CleanupManifestItem).where(CleanupManifestItem.job_id == job.id)).all()
    by_id = {i.record_id: i for i in items}
    for c in selected:
        assert by_id[c].state == "protected" and by_id[c].reason == "in_edition_selection"
    assert by_id[run].state == "protected" and by_id[run].reason == "referenced_by_edition"
    assert by_id[old_run].state == "pending"
    assert all(by_id[c].state == "pending" for c in extra)
    assert job.protection_summary["in_edition_selection"] == len(selected)
    assert job.eligible_count == len(extra) + len(unselected_clusters(session, selected)) + 1


def test_record_that_becomes_protected_after_staging_is_skipped(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    add_staged_item(session, extra[0])  # new sighting: the story is back in the pipeline
    other_week = WEEK_START + timedelta(days=7)
    add_run(session, week=other_week, candidates=[extra[1]])
    from tests.factories import edition as make_edition

    make_edition(session, week_start=other_week, status="draft", published_at=None, slug="2026-w46", number=99)
    service.test_clock.now = AFTER_HOLD
    service.scheduled_run()
    job = job_for(session, eid)
    assert job.status == "completed"
    assert exists(session, StoryAnalysisRecord, extra[0]) and exists(session, StoryAnalysisRecord, extra[1])
    assert not exists(session, StoryAnalysisRecord, extra[2])
    items = {
        i.record_id: i for i in session.scalars(select(CleanupManifestItem).where(CleanupManifestItem.job_id == job.id))
    }
    assert (items[extra[0]].state, items[extra[0]].reason) == ("skipped", "still_in_pipeline")
    assert (items[extra[1]].state, items[extra[1]].reason) == ("protected", "active_edition_candidate")
    assert "records_skipped" in actions(session, job.id)


def test_story_ranked_in_another_week_is_left_to_that_week(week):
    session, eid, selected, extra, *_ = week
    session.execute(
        update(StoryVerificationRecord)
        .where(StoryVerificationRecord.cluster_id == extra[3])
        .values(edition_start=WEEK_START + timedelta(days=7))
    )
    add_run(session, candidates=[extra[3]])  # still listed by an old run of this week
    run_to_completion(make_cleanup(session), session)
    assert exists(session, StoryAnalysisRecord, extra[3])


def test_recently_touched_records_are_skipped(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    # The pipeline touches the story again an hour before deletion (updated_at set as Stage 3.3 would).
    session.execute(text("ALTER TABLE story_analyses DISABLE TRIGGER trg_story_analyses_updated_at"))
    session.execute(
        text("UPDATE story_analyses SET updated_at = :t WHERE cluster_id = :c"),
        {"t": AFTER_HOLD - timedelta(hours=1), "c": extra[0]},
    )
    session.execute(text("ALTER TABLE story_analyses ENABLE TRIGGER trg_story_analyses_updated_at"))
    service.test_clock.now = AFTER_HOLD
    service.scheduled_run()
    assert exists(session, StoryAnalysisRecord, extra[0])
    assert not exists(session, StoryAnalysisRecord, extra[1])


def test_database_guard_is_never_bypassed(week, monkeypatch):
    """Even if the application-level checks were wrong, the database refuses and the record is kept."""
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    job = job_for(session, eid)
    # Force a selected story into the deletion list and switch the application checks off.
    session.execute(
        update(CleanupManifestItem)
        .where(CleanupManifestItem.job_id == job.id, CleanupManifestItem.record_id == selected[0])
        .values(state="pending", reason=None, processed_at=None)
    )
    monkeypatch.setattr(el, "story_protections", lambda s, ids, *a, **k: dict.fromkeys(ids))
    service.test_clock.now = AFTER_HOLD
    service.scheduled_run()
    assert exists(session, StoryAnalysisRecord, selected[0])  # kept by the foreign key / trigger
    item = session.scalar(
        select(CleanupManifestItem).where(
            CleanupManifestItem.job_id == job.id, CleanupManifestItem.record_id == selected[0]
        )
    )
    assert (item.state, item.reason) == ("protected", "database_protected")
    assert not exists(session, StoryAnalysisRecord, extra[0])  # the rest of the batch still went through
    assert job_for(session, eid).status == "completed"


def test_database_refuses_to_delete_published_content(week):
    session, eid, selected, *_ = week
    for sql in (
        "DELETE FROM editions WHERE id = :id",
        "DELETE FROM stories WHERE edition_id = :id",
        "DELETE FROM edition_items WHERE edition_id = :id",
        "DELETE FROM edition_revisions WHERE edition_id = :id",
    ):
        with pytest.raises(Exception, match="cannot be deleted|append-only|violates foreign key"):
            with session.begin_nested():
                session.execute(text(sql), {"id": eid})
    # Even after its edition_items row is gone (e.g. a later admin edit), an analysis in a revision is protected.
    with pytest.raises(Exception, match="part of an edition revision"):
        with session.begin_nested():
            session.execute(text("SET LOCAL session_replication_role = DEFAULT"))
            session.execute(text("ALTER TABLE edition_items DISABLE TRIGGER ALL"))
            session.execute(text("DELETE FROM edition_items WHERE cluster_id = :c"), {"c": selected[0]})
            session.execute(text("ALTER TABLE edition_items ENABLE TRIGGER ALL"))
            session.execute(text("DELETE FROM story_analyses WHERE cluster_id = :c"), {"c": selected[0]})
    with pytest.raises(Exception, match="violates foreign key"):
        with session.begin_nested():
            run_id = session.get(Edition, eid).ranking_run_id
            session.execute(text("DELETE FROM ranking_runs WHERE id = :id"), {"id": run_id})


# ------------------------------------------------------------------------------------------- batches, idempotency
def test_bounded_batches_continue_on_the_next_run(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session, execution={"batch_size": 2, "max_batches_per_run": 1})
    service.test_clock.now = LATER
    service.scheduled_run()
    total = job_for(session, eid).eligible_count
    service.test_clock.now = AFTER_HOLD
    runs = 0
    while job_for(session, eid).status != "completed":
        report = service.scheduled_run()
        assert report.deleted <= 2
        runs += 1
        assert runs < 10
    assert runs >= (total + 1) // 2
    assert job_for(session, eid).deleted_count == total


def test_repeated_and_duplicate_runs_are_idempotent(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    run_to_completion(service, session)
    job = job_for(session, eid)
    snapshot = (job.deleted_count, job.status, len(actions(session, job.id)))
    for _ in range(3):
        report = service.scheduled_run()
        assert report.planned == 0 and report.deleted == 0
    job = job_for(session, eid)
    assert (job.deleted_count, job.status, len(actions(session, job.id))) == snapshot
    assert session.scalar(select(func.count()).select_from(CleanupJob).where(CleanupJob.edition_id == eid)) == 1
    assert service._create_week_job(eid, SCHEDULER, LATER) is None


def test_concurrent_pass_is_refused_by_the_run_lock(week):
    session, eid, *_ = week
    service = make_cleanup(session)
    lock = service.jobs.start_job(CLEANUP_JOB_KEY, "cleanup", LATER)
    assert lock is not None
    report = service.scheduled_run()
    assert report.status == "skipped" and job_for(session, eid) is None


def test_deletion_waits_while_a_pipeline_stage_runs(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    session.add(IngestionJob(job_type="verification", source_key="stage-3.4-ranking", status="running",
                             started_at=AFTER_HOLD))  # fmt: skip
    session.flush()
    service.test_clock.now = AFTER_HOLD
    report = service.scheduled_run()
    assert report.deferred and job_for(session, eid).status == "staged"
    assert all(exists(session, StoryAnalysisRecord, c) for c in extra)
    assert "deferred" in actions(session, job_for(session, eid).id)


def test_interrupted_run_is_resumed_without_repeating_deletions(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session, execution={"batch_size": 2, "max_batches_per_run": 1})
    service.test_clock.now = LATER
    service.scheduled_run()
    service.test_clock.now = AFTER_HOLD
    service.scheduled_run()
    done = job_for(session, eid).deleted_count
    # Simulate a crash in the middle of a pass: the job is left "running".
    session.execute(update(CleanupJob).where(CleanupJob.edition_id == eid).values(status="running",
                                                                                  last_run_at=AFTER_HOLD))  # fmt: skip
    service.test_clock.now = AFTER_HOLD + timedelta(hours=2)
    while job_for(session, eid).status != "completed":
        service.scheduled_run()
    job = job_for(session, eid)
    assert job.deleted_count >= done
    deleted = session.scalar(
        select(func.count())
        .select_from(CleanupManifestItem)
        .where(CleanupManifestItem.job_id == job.id, CleanupManifestItem.state == "deleted")
    )
    assert deleted == job.deleted_count
    assert "retry_scheduled" in actions(session, job.id)


def test_failure_is_retried_with_backoff_then_completes(week, monkeypatch):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session, execution={"retry_backoff_minutes": 30})
    service.test_clock.now = LATER
    service.scheduled_run()
    original = service._delete_in_tx

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(service, "_delete_in_tx", boom)
    service.test_clock.now = AFTER_HOLD
    report = service.scheduled_run()
    job = job_for(session, eid)
    assert report.status == "failed" and job.status == "failed" and job.retry_count == 1
    assert job.next_retry_at == AFTER_HOLD + timedelta(minutes=30)
    assert job.last_error == "RuntimeError during deletion."  # safe: no exception text
    assert all(exists(session, StoryAnalysisRecord, c) for c in extra)  # nothing half-deleted
    monkeypatch.setattr(service, "_delete_in_tx", original)
    service.test_clock.now = AFTER_HOLD + timedelta(minutes=29)
    service.scheduled_run()
    assert job_for(session, eid).status == "failed"  # backoff not over
    service.test_clock.now = AFTER_HOLD + timedelta(minutes=31)
    service.scheduled_run()
    assert job_for(session, eid).status == "completed"


def test_retries_are_bounded(week, monkeypatch):
    session, eid, *_ = week
    service = make_cleanup(session, execution={"max_retries": 2, "retry_backoff_minutes": 1})
    service.test_clock.now = LATER
    service.scheduled_run()
    monkeypatch.setattr(service, "_delete_in_tx", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    now = AFTER_HOLD
    for _ in range(6):
        service.test_clock.now = now
        service.scheduled_run()
        now += timedelta(hours=1)
    job = job_for(session, eid)
    assert job.status == "failed" and job.retry_count == 3 and job.next_retry_at is None


def test_unexpected_deletion_result_is_never_marked_complete(week, monkeypatch):
    session, eid, *_ = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    monkeypatch.setattr(service, "_leftovers", lambda s, job_id: 1)
    service.test_clock.now = AFTER_HOLD
    service.scheduled_run()
    job = job_for(session, eid)
    assert job.status == "failed" and "still present" in job.last_error


# ------------------------------------------------------------------------------------------- admin operations
def test_cancel_retry_and_reopen(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    job = job_for(session, eid)
    service.cancel(job.id, ADMIN)
    service.test_clock.now = AFTER_HOLD
    service.scheduled_run()
    assert job_for(session, eid).status == "cancelled"
    assert all(exists(session, StoryAnalysisRecord, c) for c in extra)
    with pytest.raises(CleanupError):
        service.retry(job.id, ADMIN)  # only failed jobs
    out = service.plan_edition(eid, ADMIN)  # manual trigger reopens with a fresh hold
    assert out["status"] == "staged"
    assert job_for(session, eid).execute_after >= AFTER_HOLD + timedelta(hours=24)
    service.scheduled_run()
    assert job_for(session, eid).status == "staged"
    service.test_clock.now = AFTER_HOLD + timedelta(hours=25)
    service.scheduled_run()
    assert job_for(session, eid).status == "completed"
    with pytest.raises(CleanupError):
        service.cancel(job.id, ADMIN)
    acts = actions(session, job.id)
    assert "job_cancelled" in acts and "job_reopened" in acts
    entry = session.scalar(
        select(CleanupAuditEntry).where(CleanupAuditEntry.action == "job_cancelled", CleanupAuditEntry.job_id == job.id)
    )
    assert entry.actor_id == ADMIN.user_id and entry.policy_version == service.config.version


def test_invalid_ids_are_rejected(week):
    session, *_ = week
    service = make_cleanup(session)
    for bad in ("not-a-uuid", "1; DROP TABLE editions", ""):
        with pytest.raises(CleanupError) as exc:
            service.cancel(bad, ADMIN)
        assert exc.value.status_code == 422
    with pytest.raises(CleanupError) as exc:
        service.retry(uuid.uuid4(), ADMIN)
    assert exc.value.status_code == 404
    with pytest.raises(CleanupError):
        service.plan_edition(uuid.uuid4(), ADMIN)
    with pytest.raises(CleanupError):
        service.job_detail(uuid.uuid4())


def test_manual_runs_are_rate_limited(week):
    session, *_ = week
    service = make_cleanup(session, execution={"manual_runs_per_hour": 2})
    service.run_now(ADMIN)
    service.run_now(ADMIN)
    with pytest.raises(CleanupError) as exc:
        service.run_now(ADMIN)
    assert exc.value.status_code == 429
    service.run_now(Actor("admin", uuid.UUID("00000000-0000-4000-8000-0000000000a2")))  # per admin


def test_job_detail_and_dry_run(week):
    session, eid, selected, extra, *_ = week
    service = make_cleanup(session)
    dry = service.dry_run()
    assert dry["weeks"][0]["publication"]["ok"] is True
    assert dry["weeks"][0]["stories"]["kept"]["in_edition_selection"] == len(selected)
    assert job_for(session, eid) is None  # the dry run wrote nothing
    assert session.scalar(select(func.count()).select_from(CleanupAuditEntry)) == 0
    service.test_clock.now = LATER
    service.scheduled_run()
    job = job_for(session, eid)
    detail = service.job_detail(job.id)
    assert detail["editionSlug"] == "2026-w45" and detail["status"] == "staged"
    assert {"target": "story_cluster", "state": "protected", "reason": "in_edition_selection",
            "count": len(selected)} in detail["manifest"]  # fmt: skip
    assert detail["audit"][0]["action"] == "manifest_staged"
    assert all(i["state"] == "protected" for i in service.job_detail(job.id, state="protected")["items"])
    policy = service.policy()
    assert policy["version"] == service.config.version and policy["existingStage31"]["stagedItemRetentionDays"]


# ------------------------------------------------------------------------------------------- orphans
def test_orphan_sweep_removes_only_dead_expired_data(week):
    session, eid, selected, extra, *_ = week
    never_ranked = uuid.uuid4()
    pre_launch = uuid.uuid4()
    still_valid = uuid.uuid4()
    in_pipeline = uuid.uuid4()
    lone_verification = uuid.uuid4()
    add_working_data(session, never_ranked, verification=False, expires_at=OLD, touched_at=OLD)
    add_working_data(session, pre_launch, week=WEEK_START - timedelta(days=14), expires_at=OLD, touched_at=OLD)
    add_working_data(session, still_valid, verification=False, touched_at=OLD,
                     expires_at=LATER + timedelta(days=30))  # fmt: skip
    add_working_data(session, in_pipeline, verification=False, expires_at=OLD, touched_at=OLD)
    add_staged_item(session, in_pipeline)
    add_working_data(session, lone_verification, analysis=False, week=WEEK_START - timedelta(days=7),
                     expires_at=OLD, touched_at=OLD)  # fmt: skip
    # An unpublished future week's verification is never an orphan.
    future = uuid.uuid4()
    add_working_data(session, future, week=WEEK_START + timedelta(days=21), expires_at=OLD, touched_at=OLD)
    service = make_cleanup(session)
    # Make the first edition week the edition's week, so earlier weeks count as "before the first edition".
    service.edition_config = service.edition_config.model_copy(
        update={"schedule": service.edition_config.schedule.model_copy(update={"start_week": WEEK_START})}
    )
    run_to_completion(service, session)
    sweep = session.scalar(select(CleanupJob).where(CleanupJob.kind == "expired_orphans"))
    assert sweep is not None and sweep.status == "completed"
    assert not exists(session, StoryAnalysisRecord, never_ranked)
    assert not exists(session, StoryAnalysisRecord, pre_launch)
    assert not exists(session, StoryVerificationRecord, lone_verification)
    assert exists(session, StoryAnalysisRecord, still_valid)
    assert exists(session, StoryAnalysisRecord, in_pipeline)
    assert exists(session, StoryAnalysisRecord, future)
    assert all(exists(session, StoryAnalysisRecord, c) for c in selected)
    # At most one sweep per week, however often the scheduler runs.
    count = select(func.count()).select_from(CleanupJob).where(CleanupJob.kind == "expired_orphans")
    service.scheduled_run()
    before = session.scalar(count)
    for _ in range(3):
        service.scheduled_run()
    assert session.scalar(count) == before


def test_selected_stories_survive_any_number_of_passes(week):
    session, eid, selected, *_ = week
    service = make_cleanup(session)
    now = LATER
    for _ in range(4):
        service.test_clock.now = now
        service.scheduled_run()
        now += timedelta(days=8)
    for c in selected:
        assert exists(session, StoryAnalysisRecord, c)
    assert session.scalar(select(func.count()).select_from(EditionItem).where(EditionItem.edition_id == eid)) == len(
        selected
    )


def test_a_job_is_claimed_by_one_pass_only(week):
    session, eid, *_ = week
    service = make_cleanup(session)
    service.test_clock.now = LATER
    service.scheduled_run()
    job_id = job_for(session, eid).id
    assert service._claim(job_id, SCHEDULER, AFTER_HOLD) is not None
    assert service._claim(job_id, SCHEDULER, AFTER_HOLD) is None  # already running
    assert job_for(session, eid).status == "running"
