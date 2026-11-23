"""Stage 3.5 workflow against PostgreSQL (rolled back): generation, idempotency, retries, quota, revisions, review,
approval, publishing, public visibility and the append-only guarantees. Fake Gemini; no network."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.editions.errors import EditionError
from app.editions.service import Actor, RevisionInput, StoryInput
from app.intelligence.providers.base import GenerationResponse, ProviderError
from app.models import Edition, EditionAuditEntry, EditionItem, EditionRevision, IngestionJob, Story
from tests.edition_helpers import (
    ADMIN,
    MONDAY,
    OTHER,
    WEEK_END_UTC,
    WEEK_START,
    ScriptedGemini,
    edition_config,
    make_service,
    pipeline,
    seed_admins,
    seed_pipeline,
)

pytestmark = pytest.mark.usefixtures("engine")


@pytest.fixture
def seeded(db_session):
    seed_admins(db_session)
    run = seed_pipeline(db_session)
    return db_session, run


def actions(session, edition_id):
    return [
        a.action
        for a in session.scalars(
            select(EditionAuditEntry).where(EditionAuditEntry.edition_id == edition_id).order_by(EditionAuditEntry.id)
        )
    ]


def generate(service):
    return service.generate_week(WEEK_START, actor=Actor.scheduler())


def revision_input(detail, **changes) -> RevisionInput:
    content = detail["content"]
    stories = changes.pop(
        "stories",
        [StoryInput(uuid.UUID(st["cluster_id"]), _text(st["text"])) for st in content["stories"]],
    )
    return RevisionInput(
        expected_revision=changes.pop("expected_revision", detail["currentRevision"]),
        title=changes.pop("title", content["title"]),
        introduction=changes.pop("introduction", content["introduction"]),
        editorial_note=changes.pop("editorial_note", content["editorial_note"]),
        stories=stories,
        acknowledge_review=changes.pop("acknowledge_review", set()),
    )


def _text(t):
    from app.editions.content import StoryText

    return StoryText(**t)


def approve(service, edition_id, actor=ADMIN):
    d = service.detail(edition_id)
    return service.approve(edition_id, actor, d["currentRevision"], d["contentHash"])


# --------------------------------------------------------------------------------------------- generation
def test_generation_creates_a_review_ready_edition_from_the_shortlist(seeded):
    session, run = seeded
    service = make_service(session)
    report = generate(service)
    assert report.status == "generated", report.message
    edition = session.get(Edition, report.edition_id)
    assert edition.status == "review_required" and edition.current_revision == 1
    assert edition.slug == "2026-w45" and edition.number == 1 and edition.ranking_run_id == run.id
    assert edition.week_start == WEEK_START and edition.timezone == "Asia/Kolkata"
    assert edition.published_at is None and edition.generation["mode"] == "ai"
    items = session.scalars(
        select(EditionItem).where(EditionItem.edition_id == edition.id).order_by(EditionItem.position)
    ).all()
    shortlist = run.result["rankedCandidateIds"]
    assert [str(i.cluster_id) for i in items] == shortlist  # Stage 3.4 order preserved
    assert all(i.selection_source == "ranking" and i.eligibility == "eligible" for i in items)
    rev = session.scalar(select(EditionRevision).where(EditionRevision.edition_id == edition.id))
    assert rev.origin == "generated" and rev.created_by is None
    assert actions(session, edition.id) == ["generation_started", "revision_created", "generated", "approval_requested"]
    job = session.get(IngestionJob, edition.generation_job_id)
    assert job.job_type == "edition_generation" and job.status == "succeeded"


def test_ai_story_ids_and_urls_are_never_trusted(seeded):
    session, _ = seeded
    service = make_service(session)
    edition_id = generate(service).edition_id
    detail = service.detail(edition_id)
    rows, _ = pipeline()
    sources = {r["cluster_id"]: {s["originalUrl"] for s in r["analysis"]["sources"]} for r in rows if r["analysis"]}
    for st in detail["content"]["stories"]:
        assert st["source_url"] in sources[uuid.UUID(st["cluster_id"])]
    assert not [i for i in detail["issues"] if i["severity"] == "error"]


def test_scheduler_is_idempotent_and_never_duplicates(seeded):
    session, _ = seeded
    service = make_service(session)
    first = service.scheduled_run()
    assert [r.status for r in first] == ["generated"]
    second = service.scheduled_run()
    assert [r.status for r in second] == ["skipped"] and "already exists" in second[0].message
    assert session.scalar(select(func.count()).select_from(Edition).where(Edition.week_start == WEEK_START)) == 1
    assert len(service.provider.requests) == 1


def test_running_week_is_never_generated(seeded):
    session, _ = seeded
    service = make_service(session, clock=lambda: WEEK_END_UTC - timedelta(minutes=1))
    assert generate(service).status == "skipped"
    assert WEEK_START not in [r.week_start for r in service.scheduled_run()]
    assert session.scalar(select(func.count()).select_from(Edition)) == 0


def test_ranking_made_before_the_week_ended_is_not_final(db_session):
    seed_admins(db_session)
    seed_pipeline(db_session, processed_at=WEEK_END_UTC - timedelta(hours=2))
    calls = []
    service = make_service(db_session, rank_week=calls.append)
    report = generate(service)
    assert report.status == "waiting" and calls == [WEEK_START]  # Stage 3.4 asked to finalise the week
    assert db_session.scalar(select(func.count()).select_from(Edition)) == 0


def _incomplete_result(pending: int = 2) -> dict:
    _, ranking = pipeline()
    return {
        **ranking,
        "status": "incomplete",
        "pending": [
            {"storyClusterId": str(uuid.uuid4()), "reason": "not_analysed", "detail": "not analysed yet"}
            for _ in range(pending)
        ],
    }


def test_incomplete_ranking_waits_during_the_grace_period(db_session):
    seed_admins(db_session)
    seed_pipeline(db_session, status="incomplete", result=_incomplete_result())
    service = make_service(db_session, clock=lambda: WEEK_END_UTC + timedelta(hours=47))
    assert generate(service).status == "waiting"
    assert db_session.scalar(select(func.count()).select_from(Edition)) == 0


def test_incomplete_ranking_is_used_after_the_grace_period_and_flagged(db_session):
    seed_admins(db_session)
    seed_pipeline(db_session, status="incomplete", result=_incomplete_result(3))
    service = make_service(db_session, clock=lambda: WEEK_END_UTC + timedelta(hours=49))
    report = generate(service)
    assert report.status == "generated", report.message
    detail = service.detail(report.edition_id)
    assert detail["content"]["composition"]["rankingPending"] == 3
    assert "incomplete_ranking" in [i["code"] for i in detail["issues"]]
    assert detail["status"] == "review_required"  # a warning, not a blocker


def test_incomplete_ranking_is_never_used_when_disabled(db_session):
    seed_admins(db_session)
    seed_pipeline(db_session, status="incomplete", result=_incomplete_result())
    cfg = edition_config(schedule={"accept_incomplete_ranking_after_hours": 0})
    service = make_service(db_session, config=cfg, clock=lambda: WEEK_END_UTC + timedelta(days=5))
    assert generate(service).status == "waiting"


def test_incomplete_ranking_made_before_the_week_ended_is_never_used(db_session):
    seed_admins(db_session)
    seed_pipeline(
        db_session,
        status="incomplete",
        result=_incomplete_result(),
        processed_at=WEEK_END_UTC - timedelta(hours=1),
    )
    service = make_service(db_session, clock=lambda: WEEK_END_UTC + timedelta(days=3))
    assert generate(service).status == "waiting"


def test_missing_ranking_waits_and_scheduler_failure_is_contained(db_session):
    seed_admins(db_session)

    def broken(_):
        raise RuntimeError("stage 3.4 down")

    report = generate(make_service(db_session, rank_week=broken))
    assert report.status == "waiting"
    entry = db_session.scalar(select(EditionAuditEntry).where(EditionAuditEntry.action == "generation_deferred"))
    assert entry is not None and entry.edition_id is None


def test_empty_shortlist_gives_a_draft_that_cannot_be_approved(db_session):
    seed_admins(db_session)
    _, result = pipeline()
    seed_pipeline(db_session, result={**result, "shortlist": [], "rankedCandidateIds": []})
    service = make_service(db_session)
    report = generate(service)
    edition = db_session.get(Edition, report.edition_id)
    assert edition.status == "draft"
    detail = service.detail(edition.id)
    assert any(i["code"] == "no_stories" for i in detail["issues"])
    assert service.provider.requests == []  # nothing to compose, no quota used
    with pytest.raises(EditionError) as exc:
        service.submit(edition.id, ADMIN, edition.current_revision)
    assert exc.value.code == "edition_invalid"


def test_fewer_and_more_stories_than_target(seeded):
    session, run = seeded
    service = make_service(session, config=edition_config(selection={"target_stories": 1, "min_stories": 3}))
    edition_id = generate(service).edition_id
    detail = service.detail(edition_id)
    assert len(detail["content"]["stories"]) == 1  # capped at the target, top of the shortlist
    assert detail["content"]["stories"][0]["cluster_id"] == run.result["rankedCandidateIds"][0]
    assert any(i["code"] == "few_stories" for i in detail["issues"])  # warned, not padded
    assert detail["status"] == "review_required"


def test_invalid_ai_output_falls_back_to_stage33_text(seeded):
    session, _ = seeded
    bad = lambda r: GenerationResponse(content="{oops", model=r.model, finish_reason="stop")  # noqa: E731
    service = make_service(session, provider=ScriptedGemini(bad))
    report = generate(service)
    detail = service.detail(report.edition_id)
    assert detail["generation"]["mode"] == "stage33_text"
    assert any(i["code"] == "not_ai_composed" for i in detail["issues"])
    for st in detail["content"]["stories"]:
        assert st["text"] == st["original"]


def test_without_a_key_the_edition_still_generates(seeded):
    session, _ = seeded
    report = generate(make_service(session, provider=False))
    assert report.status == "generated"


def test_quota_exhaustion_pauses_without_using_an_attempt_and_resumes(seeded):
    session, _ = seeded
    quota = ProviderError("quota", "daily quota used up", status_code=429)
    clock = {"now": MONDAY}
    service = make_service(session, provider=ScriptedGemini(quota), clock=lambda: clock["now"])
    report = generate(service)
    assert report.status == "quota_limited"
    edition = session.get(Edition, report.edition_id)
    assert edition.status == "generation_failed" and edition.generation_attempts == 0
    assert edition.next_attempt_at > MONDAY and edition.generation["pendingReason"] == "quota"
    assert service.scheduled_run()[0].status == "skipped"  # waits for the reset
    clock["now"] = edition.next_attempt_at + timedelta(minutes=1)
    service.provider.steps = [service.provider.steps[0].__class__ and (lambda r: _ok(r))]
    resumed = service.scheduled_run()
    assert resumed[0].status == "generated"
    assert session.get(Edition, report.edition_id).status == "review_required"


def _ok(request):
    from tests.edition_helpers import composer_answer

    return composer_answer(request)


def test_crash_is_recorded_with_backoff_and_max_attempts(seeded, monkeypatch):
    session, _ = seeded
    clock = {"now": MONDAY}
    service = make_service(session, clock=lambda: clock["now"], config=edition_config(generation={"max_attempts": 2}))

    def boom(*a, **k):
        raise ValueError("bad data")

    monkeypatch.setattr("app.editions.service.select_candidates", boom)
    report = generate(service)
    assert report.status == "failed" and "ValueError" in report.message
    edition = session.get(Edition, report.edition_id)
    assert edition.status == "generation_failed" and edition.generation_attempts == 1
    assert edition.last_error and "bad data" not in edition.last_error  # no internal detail
    assert service.scheduled_run()[0].status == "skipped"  # backoff
    clock["now"] += timedelta(hours=3)
    assert service.scheduled_run()[0].status == "failed"  # second attempt
    clock["now"] += timedelta(days=1)
    skipped = service.scheduled_run()[0]
    assert skipped.status == "skipped" and "admin must retry" in skipped.message
    monkeypatch.undo()
    retried = service.regenerate(edition.id, ADMIN, edition.current_revision)  # manual admin retry
    assert retried.status == "generated"
    assert "retry_triggered" in actions(session, edition.id)


def test_concurrent_generation_lock(seeded):
    session, _ = seeded
    service = make_service(session)
    job = service.jobs.start_job("stage-3.5-edition", "edition_generation", MONDAY)  # someone else is running
    assert job is not None
    report = generate(service)
    assert report.status == "skipped" and "Another" in report.message


# --------------------------------------------------------------------------------------------- revisions
@pytest.fixture
def generated(seeded):
    session, run = seeded
    service = make_service(session)
    edition_id = generate(service).edition_id
    return session, service, edition_id


def test_admin_edit_creates_a_revision_and_preserves_the_original(generated):
    session, service, eid = generated
    detail = service.detail(eid)
    first = detail["content"]["stories"][0]
    stories = [StoryInput(uuid.UUID(st["cluster_id"]), _text(st["text"])) for st in detail["content"]["stories"]]
    stories[0] = StoryInput(
        stories[0].cluster_id, stories[0].text.model_copy(update={"headline": "A clearer headline for students"})
    )
    out = service.save_revision(
        eid, ADMIN, revision_input(detail, stories=stories, introduction="A new introduction for this week's edition.")
    )
    assert out["changed"] and out["revision"] == 2
    after = service.detail(eid)
    assert after["content"]["stories"][0]["text"]["headline"] == "A clearer headline for students"
    assert after["content"]["stories"][0]["original"] == first["original"]  # generated original kept
    revs = session.scalars(
        select(EditionRevision).where(EditionRevision.edition_id == eid).order_by(EditionRevision.revision)
    ).all()
    assert [r.revision for r in revs] == [1, 2] and revs[1].created_by == ADMIN.user_id
    assert revs[1].changes["editionFields"] == ["introduction"]
    assert revs[1].changes["edited"][0]["fields"] == ["headline"]
    assert "content_edited" in actions(session, eid)
    # Saving the same content again is a no-op.
    assert service.save_revision(eid, ADMIN, revision_input(service.detail(eid)))["changed"] is False


def test_remove_reorder_and_add_stories_are_tracked(generated):
    session, service, eid = generated
    detail = service.detail(eid)
    ids = [st["cluster_id"] for st in detail["content"]["stories"]]
    reordered = list(reversed(detail["content"]["stories"]))
    stories = [StoryInput(uuid.UUID(st["cluster_id"]), _text(st["text"])) for st in reordered]
    service.save_revision(eid, ADMIN, revision_input(detail, stories=stories))
    assert "story_reordered" in actions(session, eid)
    detail = service.detail(eid)
    service.save_revision(eid, ADMIN, revision_input(detail, stories=stories[:1]))
    assert "story_removed" in actions(session, eid)
    cands = service.candidates(eid)
    kept = str(stories[0].cluster_id)
    assert cands and all(c["clusterId"] != kept for c in cands) and len(ids) >= 2
    new = cands[0]
    detail = service.detail(eid)
    stories = stories[:1] + [StoryInput(uuid.UUID(new["clusterId"]), _text(new["text"]))]
    service.save_revision(eid, ADMIN, revision_input(detail, stories=stories))
    after = service.detail(eid)
    added = after["content"]["stories"][1]
    assert added["selection"]["source"] == "admin_added" and added["cluster_id"] == new["clusterId"]
    assert "story_added" in actions(session, eid)
    item = session.scalar(select(EditionItem).where(EditionItem.cluster_id == uuid.UUID(new["clusterId"])))
    assert item.selection_source == "admin_added" and item.position == 2


def test_only_ranking_candidates_can_be_added(generated):
    session, service, eid = generated
    detail = service.detail(eid)
    fake = StoryInput(uuid.uuid4(), _text(detail["content"]["stories"][0]["text"]))
    with pytest.raises(EditionError) as exc:
        service.save_revision(eid, ADMIN, revision_input(detail, stories=[fake]))
    assert exc.value.code == "not_a_candidate"


def test_review_required_candidates_need_an_explicit_acknowledgement(db_session):
    seed_admins(db_session)
    _, result = pipeline()
    data = {**result, "candidates": [dict(c) for c in result["candidates"]]}
    target = next(c for c in data["candidates"] if c["storyClusterId"] not in result["rankedCandidateIds"])
    target["eligibilityStatus"] = "review_required"
    seed_pipeline(db_session, result=data)
    service = make_service(db_session)
    eid = generate(service).edition_id
    detail = service.detail(eid)
    cand = next(c for c in service.candidates(eid) if c["clusterId"] == target["storyClusterId"])
    assert cand["eligibility"] == "review_required"
    stories = [StoryInput(uuid.UUID(st["cluster_id"]), _text(st["text"])) for st in detail["content"]["stories"]]
    stories.append(StoryInput(uuid.UUID(cand["clusterId"]), _text(cand["text"])))
    with pytest.raises(EditionError) as exc:
        service.save_revision(eid, ADMIN, revision_input(detail, stories=stories))
    assert exc.value.code == "review_not_acknowledged"
    out = service.save_revision(
        eid, ADMIN, revision_input(detail, stories=stories, acknowledge_review={uuid.UUID(cand["clusterId"])})
    )
    issues = out["issues"]
    assert any(i["code"] == "review_required_story" and i["severity"] == "warning" for i in issues)
    item = db_session.scalar(select(EditionItem).where(EditionItem.cluster_id == uuid.UUID(cand["clusterId"])))
    assert item.review_acknowledged_by == ADMIN.user_id


def test_stale_revision_is_rejected(generated):
    _, service, eid = generated
    detail = service.detail(eid)
    service.save_revision(eid, ADMIN, revision_input(detail, title="Another good title for this week"))
    for call in (
        lambda: service.save_revision(eid, OTHER, revision_input(detail, title="Conflicting title for this week")),
        lambda: service.approve(eid, ADMIN, detail["currentRevision"], detail["contentHash"]),
        lambda: service.reject(eid, ADMIN, detail["currentRevision"], "no", to_draft=False),
    ):
        with pytest.raises(EditionError) as exc:
            call()
        assert exc.value.status_code in (409, 422)


# --------------------------------------------------------------------------------------------- review and approval
def test_approval_requires_review_state_exact_revision_and_hash(generated):
    session, service, eid = generated
    d = service.detail(eid)
    with pytest.raises(EditionError) as exc:
        service.approve(eid, ADMIN, d["currentRevision"], "0" * 64)
    assert exc.value.code == "stale_revision"
    with pytest.raises(EditionError) as exc:
        service.approve(eid, Actor("scheduler"), d["currentRevision"], d["contentHash"])
    assert exc.value.status_code == 403
    out = service.approve(eid, ADMIN, d["currentRevision"], d["contentHash"])
    assert out["status"] == "approved"
    edition = session.get(Edition, eid)
    assert edition.approved_by == ADMIN.user_id and edition.approved_revision == 1
    assert edition.approved_content_hash == d["contentHash"] and edition.approved_at is not None
    # Approving the same revision again is idempotent.
    assert service.approve(eid, OTHER, 1, d["contentHash"])["alreadyApproved"] is True
    assert actions(session, eid).count("approved") == 1


def test_draft_cannot_be_approved_and_rejected_flow(generated):
    session, service, eid = generated
    d = service.detail(eid)
    service.reject(eid, ADMIN, d["currentRevision"], "Needs a better intro", to_draft=True)
    assert session.get(Edition, eid).status == "draft"
    d = service.detail(eid)
    with pytest.raises(EditionError) as exc:
        service.approve(eid, ADMIN, d["currentRevision"], d["contentHash"])
    assert exc.value.code == "invalid_state"
    service.submit(eid, ADMIN, d["currentRevision"])
    service.reject(eid, OTHER, d["currentRevision"], "Not this week", to_draft=False)
    edition = session.get(Edition, eid)
    assert edition.status == "rejected" and edition.review_note == "Not this week"
    with pytest.raises(EditionError):
        service.publish(eid, ADMIN, edition.current_revision)
    # Editing a rejected edition reopens it as a draft.
    d = service.detail(eid)
    service.save_revision(eid, ADMIN, revision_input(d, title="A reworked edition title here"))
    assert session.get(Edition, eid).status == "draft"
    assert {"returned_for_revision", "rejected", "approval_requested"} <= set(actions(session, eid))


def test_edit_after_approval_requires_reapproval(generated):
    session, service, eid = generated
    approve(service, eid)
    d = service.detail(eid)
    service.save_revision(
        eid, ADMIN, revision_input(d, introduction="Changed after approval, so it needs review again.")
    )
    edition = session.get(Edition, eid)
    assert edition.status == "review_required" and edition.approved_revision is None and edition.approved_by is None
    assert "approval_invalidated" in actions(session, eid)
    with pytest.raises(EditionError):
        service.publish(eid, ADMIN, edition.current_revision)
    approve(service, eid, OTHER)
    assert session.get(Edition, eid).approved_revision == 2


# --------------------------------------------------------------------------------------------- publishing
def test_publish_before_approval_is_refused(generated):
    session, service, eid = generated
    with pytest.raises(EditionError) as exc:
        service.publish(eid, ADMIN, 1)
    assert exc.value.code == "invalid_state"
    assert session.scalar(select(func.count()).select_from(Story)) == 0


def test_publish_materialises_the_snapshot_and_is_idempotent(generated, make_client):
    session, service, eid = generated
    client = make_client()
    assert client.get("/api/v1/editions/current").status_code == 404  # review editions are never public
    approve(service, eid)
    assert client.get("/api/v1/editions/current").status_code == 404  # nor approved ones
    out = service.publish(eid, ADMIN, 1)
    assert out["status"] == "published"
    edition = session.get(Edition, eid)
    assert edition.published_by == ADMIN.user_id and edition.published_revision == 1 and edition.published_at
    stories = session.scalars(select(Story).where(Story.edition_id == eid).order_by(Story.edition_position)).all()
    content = service.detail(eid)["content"]
    assert [s.original_url for s in stories] == [st["source_url"] for st in content["stories"]]
    assert stories[0].is_featured and all(s.status == "published" for s in stories)
    items = session.scalars(select(EditionItem).where(EditionItem.edition_id == eid)).all()
    assert all(i.story_id for i in items)
    again = service.publish(eid, OTHER, 1)
    assert again["alreadyPublished"] is True
    assert session.scalar(select(func.count()).select_from(Story)) == len(stories)
    body = client.get("/api/v1/editions/current").json()
    assert body["slug"] == "2026-w45" and body["introduction"] == content["introduction"]
    assert [s["sourceUrl"] for s in body["stories"]][0] == content["stories"][0]["source_url"]
    assert "finalPriorityScore" not in str(body) and "reviewFlags" not in str(body)


def test_published_content_is_read_only(generated):
    session, service, eid = generated
    approve(service, eid)
    service.publish(eid, ADMIN, 1)
    d = service.detail(eid)
    with pytest.raises(EditionError) as exc:
        service.save_revision(eid, ADMIN, revision_input(d, title="Sneaky change after publishing"))
    assert exc.value.code == "invalid_state"
    with pytest.raises(EditionError):
        service.regenerate(eid, ADMIN, 1)


def test_failed_publication_is_recoverable(generated, monkeypatch):
    session, service, eid = generated
    approve(service, eid)

    def fail(*a, **k):
        raise DBAPIError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(type(service), "_materialise", fail)
    with pytest.raises(EditionError) as exc:
        service.publish(eid, ADMIN, 1)
    assert exc.value.code == "publish_failed" and "connection" not in exc.value.message
    assert session.get(Edition, eid).status == "approved"
    assert session.scalar(select(func.count()).select_from(Story)) == 0
    assert "publish_failed" in actions(session, eid)
    monkeypatch.undo()
    assert service.publish(eid, ADMIN, 1)["status"] == "published"


def test_article_already_published_elsewhere_blocks_approval(generated):
    session, service, eid = generated
    approve(service, eid)
    service.publish(eid, ADMIN, 1)
    # A second edition (another week) that picks the same article cannot be approved.
    other = Edition(
        slug="2026-w46",
        number=2,
        title="Next",
        week_start=WEEK_START + timedelta(days=7),
        week_end=WEEK_START + timedelta(days=13),
        status="generating",
    )
    session.add(other)
    session.flush()
    rev1 = session.scalar(select(EditionRevision).where(EditionRevision.edition_id == eid))
    session.add(
        EditionRevision(
            edition_id=other.id, revision=1, origin="generated", content=rev1.content, content_hash=rev1.content_hash
        )
    )
    other.current_revision = 1
    other.status = "review_required"
    session.flush()
    with pytest.raises(EditionError) as exc:
        service.approve(other.id, ADMIN, 1, rev1.content_hash)
    assert any(d["type"] == "already_published" for d in exc.value.details)


# --------------------------------------------------------------------------------------------- database guarantees
def test_revisions_and_audit_log_are_append_only(generated):
    session, _, eid = generated
    for sql in (
        "UPDATE edition_revisions SET origin = 'admin_edit'",
        "DELETE FROM edition_revisions",
        "UPDATE edition_audit_log SET action = 'approved'",
        "DELETE FROM edition_audit_log",
        "TRUNCATE edition_audit_log",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with session.begin_nested():
                session.execute(text(sql))


def test_database_rejects_inconsistent_workflow_state(generated):
    session, _, eid = generated
    for sql in (
        "UPDATE editions SET status = 'approved' WHERE id = :id",  # no approval recorded
        "UPDATE editions SET status = 'published', published_at = now() WHERE id = :id",  # not from approval
        "UPDATE editions SET status = 'hacked' WHERE id = :id",
    ):
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                session.execute(text(sql), {"id": eid})


def test_one_edition_per_week_is_enforced_by_the_database(generated):
    session, _, eid = generated
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.add(
                Edition(
                    slug="dup-week",
                    number=99,
                    title="Dup",
                    week_start=WEEK_START,
                    week_end=WEEK_START + timedelta(days=6),
                    status="draft",
                )
            )
            session.flush()


def test_new_tables_have_rls_and_no_api_role_access(generated):
    session, _, _ = generated
    for table in ("editions", "editorial_admins", "edition_revisions", "edition_items", "edition_audit_log"):
        assert session.scalar(text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": table})
    with session.begin_nested():
        session.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') "
                "THEN CREATE ROLE anon NOLOGIN; END IF; END $$"
            )
        )
        session.execute(text("GRANT USAGE ON SCHEMA public TO anon"))  # as on Supabase
        session.execute(text("SET LOCAL ROLE anon"))
        for table in ("editions", "edition_revisions", "edition_audit_log", "editorial_admins"):
            with pytest.raises(DBAPIError, match="permission denied"):
                with session.begin_nested():
                    session.execute(text(f"SELECT * FROM {table}"))  # noqa: S608 - fixed table names
        session.execute(text("RESET ROLE"))
