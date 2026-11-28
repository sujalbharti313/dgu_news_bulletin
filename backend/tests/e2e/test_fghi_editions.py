"""Phase 4.2 Scenarios F-I: weekly edition generation, admin review and editing, the approval state machine and
publication, over the real admin HTTP API and the real schema (local test PostgreSQL, rolled back).

Admin sign-in uses a fake Supabase token verifier (test-only admin accounts); the ranking is a real Stage 3.4
result of the shared test week (2-8 Nov 2026); Gemini composition is a scripted double. Nothing is published
anywhere but the rolled-back test database.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.editions.auth import VerifiedUser
from app.editions.config import load_edition_config
from app.editions.schedule import due_at, due_weeks, edition_slug, week_for
from app.main import create_app
from app.models import Edition, EditionAuditEntry, EditionItem, EditorialAdmin, Story
from app.ranking.window import EditionWindow
from tests.conftest import TEST_DATABASE_URL, make_settings
from tests.edition_helpers import (
    ADMIN_ID,
    OTHER_ADMIN_ID,
    WEEK_START,
    SavepointDatabase,
    edition_config,
    make_service,
    seed_admins,
    seed_pipeline,
)

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")

TOKEN, OTHER, STRANGER, EXPIRED = "t.admin.sig", "t.second.sig", "t.student.sig", "t.expired.sig"
INGESTION_TOKEN = "i" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SCHEDULE = load_edition_config("config/edition.toml").schedule


class Verifier:
    users = {
        TOKEN: VerifiedUser(ADMIN_ID, "editor@example.com"),
        OTHER: VerifiedUser(OTHER_ADMIN_ID, "second@example.com"),
        STRANGER: VerifiedUser(uuid.uuid4(), "student@example.com"),
    }

    def verify(self, token):
        return self.users.get(token)  # EXPIRED and anything else: Supabase says the token is not valid


def make_api(db_session, **cfg):
    seed_admins(db_session)
    seed_pipeline(db_session)
    service = make_service(db_session, config=edition_config(**cfg) if cfg else None)
    settings = make_settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        ingestion_admin_token=INGESTION_TOKEN,
        admin_write_rate_per_minute=500,
    )
    app = create_app(settings, database=SavepointDatabase(settings, db_session))
    app.state.admin_token_verifier = Verifier()
    app.state.edition_service_factory = lambda s, d: service
    client = TestClient(app)
    from app.editions.service import Actor

    report = service.generate_week(WEEK_START, actor=Actor.scheduler())
    client.eid = str(report.edition_id)
    client.service = service
    client.session = db_session
    return client


@pytest.fixture
def api(db_session):
    return make_api(db_session)


def get(api, path="", token=TOKEN):
    return api.get(f"/api/v1/admin/editions/{api.eid}{path}", headers={"Authorization": f"Bearer {token}"})


def post(api, action, body, token=TOKEN):
    return api.post(
        f"/api/v1/admin/editions/{api.eid}/{action}", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def payload(detail, *, stories=None, **changes):
    def story(st):
        t = st["text"]
        return {
            "clusterId": st["cluster_id"],
            "headline": t["headline"],
            "summary": t["summary"],
            "technicalExplanation": t["technical_explanation"],
            "whyItMatters": t["why_it_matters"],
            "studentRelevance": t["student_relevance"],
            "projectIdea": t["project_idea"],
        }

    c = detail["content"]
    body = {
        "expectedRevision": detail["currentRevision"],
        "title": c["title"],
        "introduction": c["introduction"],
        "editorialNote": c.get("editorial_note") or "",
        "stories": [story(s) for s in (stories if stories is not None else c["stories"])],
    }
    body.update(changes)
    return body


def public_text(api) -> str:
    """Everything the public API returns, as one string (to search for leaks)."""
    parts = []
    for path in (
        "/api/v1/editions",
        "/api/v1/editions/current",
        "/api/v1/stories?limit=50",
        "/api/v1/search?q=model",
        "/api/v1/briefings/latest",
    ):
        parts.append(api.get(path).text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------------- F: generation
@pytest.mark.parametrize(
    ("moment", "monday"),
    [
        (datetime(2026, 11, 29, 18, 29, 59, tzinfo=UTC), date(2026, 11, 23)),  # Sun 23:59:59 IST: still the old week
        (datetime(2026, 11, 29, 18, 30, tzinfo=UTC), date(2026, 11, 30)),  # Mon 00:00 IST: the new week
        (datetime(2026, 11, 30, 2, 0, tzinfo=UTC), date(2026, 11, 30)),  # Monday morning news
        (datetime(2026, 12, 6, 12, 0, tzinfo=UTC), date(2026, 11, 30)),  # week spanning November/December
        (datetime(2026, 12, 31, 20, 0, tzinfo=UTC), date(2026, 12, 28)),  # New Year's Eve evening UTC = 1 Jan IST
        (datetime(2027, 1, 3, 18, 29, tzinfo=UTC), date(2026, 12, 28)),  # Sun 3 Jan 23:59 IST: year-spanning week
        (datetime(2027, 1, 3, 18, 30, tzinfo=UTC), date(2027, 1, 4)),
    ],
)
def test_collection_window_boundaries_in_india_time(moment, monday):
    """E2E-F-01: Monday 00:00 to Sunday 23:59:59 Asia/Kolkata, across month and year transitions; a UTC instant is
    placed by its India date (18:30 UTC is the IST midnight)."""
    window = EditionWindow(monday, "Asia/Kolkata")
    assert window.contains(moment)
    assert not EditionWindow(monday - timedelta(days=7)).contains(moment)
    assert not EditionWindow(monday + timedelta(days=7)).contains(moment)


def test_generation_schedule_and_week_identifiers():
    """E2E-F-02: an edition is due Monday 06:00 IST after its week, never during it; ISO week slugs are right at the
    year boundary (2026 has 53 ISO weeks); asking for the running week is refused."""
    w = EditionWindow(date(2026, 12, 28))
    assert due_at(w, SCHEDULE) == datetime(2027, 1, 4, 0, 30, tzinfo=UTC)
    assert edition_slug(w) == "2026-w53" and edition_slug(EditionWindow(date(2027, 1, 4))) == "2027-w01"
    assert edition_slug(EditionWindow(date(2026, 11, 30))) == "2026-w49"
    before = datetime(2027, 1, 4, 0, 29, tzinfo=UTC)
    assert all(x.start_date != w.start_date for x in due_weeks(before, SCHEDULE))
    assert w.start_date in {x.start_date for x in due_weeks(before + timedelta(minutes=1), SCHEDULE)}
    with pytest.raises(ValueError):
        week_for("2027-01-02", datetime(2027, 1, 3, 12, 0, tzinfo=UTC), SCHEDULE)  # still running
    with pytest.raises(ValueError):
        week_for("2026-11-02", datetime(2026, 12, 1, tzinfo=UTC), SCHEDULE)  # before start_week: no back-fill


def test_generated_edition_has_required_fields_and_source_links(api):
    """E2E-F-03: the generated edition is in review, has a title and introduction, at most 10 stories in Stage 3.4
    shortlist order, and each story keeps its publisher and an original https source link recorded in Stage 3.3."""
    d = get(api).json()
    assert d["status"] == "review_required"
    c = d["content"]
    assert c["title"] and c["introduction"]
    stories = c["stories"]
    assert 1 <= len(stories) <= 10
    for st in stories:
        assert st["source_url"].startswith("https://") and st["publisher"]
        assert st["text"]["headline"] and st["text"]["summary"] and st["text"]["why_it_matters"]
    shortlist = api.service.detail(uuid.UUID(api.eid))["content"]["stories"]
    assert [s["cluster_id"] for s in shortlist] == [s["cluster_id"] for s in stories]


def test_generation_is_never_duplicated(api, db_session):
    """E2E-F-04: generating the same week again (scheduler twice, admin retry) returns the same edition; the database
    holds one edition per week."""
    from app.editions.service import Actor

    for _ in range(3):
        again = api.service.generate_week(WEEK_START, actor=Actor.scheduler())
        assert str(again.edition_id) == api.eid
    api.service.scheduled_run()
    assert db_session.scalar(select(func.count()).select_from(Edition).where(Edition.week_start == WEEK_START)) == 1


def test_short_week_is_not_padded(db_session):
    """E2E-F-05: with a target of 10 and fewer eligible shortlisted stories, the edition has only those stories."""
    api = make_api(db_session)
    n = len(get(api).json()["content"]["stories"])
    run = api.service.detail(uuid.UUID(api.eid))
    assert n < 10 and n == len(run["content"]["stories"])


def test_admin_cannot_push_an_edition_past_the_story_limit(db_session):
    """E2E-F-06: the edition may hold at most `selection.target_stories` stories (10 in production). With the
    target set to 2, the generated edition has 2 stories; an admin who adds a third ranking candidate cannot get
    that edition approved or published."""
    api = make_api(db_session, selection={"target_stories": 2, "min_stories": 1})
    d = get(api).json()
    assert len(d["content"]["stories"]) == 2
    in_edition = {s["cluster_id"] for s in d["content"]["stories"]}
    extra = next(c for c in get(api, "/candidates").json()["items"] if c["clusterId"] not in in_edition)
    body = payload(d)
    body["stories"].append({**body["stories"][0], "clusterId": extra["clusterId"]})
    saved = api.put(f"/api/v1/admin/editions/{api.eid}/content", json=body, headers=AUTH)
    assert saved.status_code == 200, saved.text  # editing may go over the limit for a moment...
    assert "too_many_stories" in {i["code"] for i in saved.json()["issues"]}
    d = get(api).json()
    assert len(d["content"]["stories"]) == 3 and d["status"] == "draft"  # ...but the edition drops back to draft
    assert post(api, "submit", {"expectedRevision": d["currentRevision"]}).status_code == 422
    r = post(api, "approve", {"expectedRevision": d["currentRevision"], "contentHash": d["contentHash"]})
    assert r.status_code == 409  # a draft is never approvable
    assert post(api, "publish", {"expectedRevision": d["currentRevision"]}).status_code == 409
    # Removing the extra story makes it valid again.
    fixed = api.put(
        f"/api/v1/admin/editions/{api.eid}/content", json=payload(d, stories=d["content"]["stories"][:2]), headers=AUTH
    )
    assert "too_many_stories" not in {i["code"] for i in fixed.json()["issues"]}
    assert post(api, "submit", {"expectedRevision": fixed.json()["revision"]}).status_code == 200


# ------------------------------------------------------------------------------------------ G: admin review
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "", None),
        ("get", "/candidates", None),
        ("get", "/preview", None),
        ("get", "/revisions/1", None),
        ("put", "/content", {}),
        ("post", "/approve", {"expectedRevision": 1, "contentHash": "0" * 64}),
        ("post", "/reject", {"expectedRevision": 1, "reason": "no"}),
        ("post", "/publish", {"expectedRevision": 1}),
        ("post", "/regenerate", {"expectedRevision": 1}),
        ("post", "/archive", {"expectedRevision": 1}),
        ("post", "/submit", {"expectedRevision": 1}),
    ],
)
def test_every_admin_endpoint_refuses_non_admins(api, method, path, body):
    """E2E-G-01: no token, an expired/invalid token and the ingestion admin token are 401; a signed-in user who is
    not an editorial admin is 403; nothing changes in the database."""
    url = f"/api/v1/admin/editions/{api.eid}{path}"
    call = getattr(api, method)
    kwargs = {"json": body} if body is not None else {}
    assert call(url, **kwargs).status_code == 401
    for token in (EXPIRED, INGESTION_TOKEN):
        assert call(url, headers={"Authorization": f"Bearer {token}"}, **kwargs).status_code == 401
    assert call(url, headers={"Authorization": f"Bearer {STRANGER}"}, **kwargs).status_code == 403
    e = api.session.get(Edition, uuid.UUID(api.eid))
    assert e.status == "review_required" and e.published_at is None and e.current_revision == 1


def test_deactivated_admin_loses_access_on_the_next_request(api, db_session):
    """E2E-G-02: an admin removed from `editorial_admins` (or deactivated) is refused at once (session still
    valid at Supabase, but the allow-list is checked on every request)."""
    assert get(api, token=OTHER).status_code == 200
    db_session.execute(
        EditorialAdmin.__table__.update().where(EditorialAdmin.user_id == OTHER_ADMIN_ID).values(is_active=False)
    )
    assert get(api, token=OTHER).status_code == 403


def test_edit_remove_reorder_are_revisions_with_audit(api):
    """E2E-G-03: editing text, removing and reordering stories each create a new revision; the original revision is
    kept unchanged and the audit log names the admin and what changed."""
    d = get(api).json()
    stories = d["content"]["stories"]
    assert len(stories) >= 2
    body = payload(d, stories=list(reversed(stories)), title="Edited title for students")
    r = api.put(f"/api/v1/admin/editions/{api.eid}/content", json=body, headers=AUTH)
    assert r.status_code == 200 and r.json()["changed"] is True and r.json()["revision"] == 2
    mid = get(api).json()
    r = api.put(
        f"/api/v1/admin/editions/{api.eid}/content",
        json=payload(mid, stories=mid["content"]["stories"][:-1]),
        headers=AUTH,
    )
    assert r.status_code == 200 and r.json()["revision"] == 3
    after = get(api).json()
    assert after["content"]["title"] == "Edited title for students"
    assert [s["cluster_id"] for s in after["content"]["stories"]] == [s["cluster_id"] for s in reversed(stories)][:-1]
    original = get(api, "/revisions/1").json()
    assert [s["cluster_id"] for s in original["content"]["stories"]] == [s["cluster_id"] for s in stories]
    actions = {a["action"] for a in after["audit"]}
    assert {"story_removed", "story_reordered", "content_edited"} <= actions
    # Saving the same content again creates nothing.
    same = api.put(f"/api/v1/admin/editions/{api.eid}/content", json={**payload(after)}, headers=AUTH)
    assert same.json()["changed"] is False and get(api).json()["currentRevision"] == 3


def test_stale_editor_cannot_overwrite_a_newer_revision(api):
    """E2E-G-04: two admins edit at once; the second save, based on revision 1, is refused (409 stale)."""
    d = get(api).json()
    assert (
        api.put(
            f"/api/v1/admin/editions/{api.eid}/content", json=payload(d, title="First editor"), headers=AUTH
        ).status_code
        == 200
    )
    r = api.put(
        f"/api/v1/admin/editions/{api.eid}/content",
        json=payload(d, title="Second editor"),
        headers={"Authorization": f"Bearer {OTHER}"},
    )
    assert r.status_code == 409
    assert get(api).json()["content"]["title"] == "First editor"


def test_only_ranking_candidates_can_be_added(api):
    """E2E-G-05: an admin cannot add an arbitrary story id; only candidates of the edition's own ranking."""
    d = get(api).json()
    body = payload(d)
    body["stories"].append({**body["stories"][0], "clusterId": str(uuid.uuid4())})
    r = api.put(f"/api/v1/admin/editions/{api.eid}/content", json=body, headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_a_candidate"


def test_drafts_never_reach_the_public_api(api):
    """E2E-G-06: in review, after an edit and after approval, no public endpoint returns the edition, its stories,
    its headlines or its slug."""
    d = get(api).json()
    headlines = [s["text"]["headline"] for s in d["content"]["stories"]]
    slug = api.session.get(Edition, uuid.UUID(api.eid)).slug

    def leaked():
        text = public_text(api)
        return [h for h in headlines if h in text] + ([slug] if slug in text else [])

    assert leaked() == [] and api.get(f"/api/v1/editions/{slug}").status_code == 404
    post(api, "approve", {"expectedRevision": 1, "contentHash": d["contentHash"]})
    assert leaked() == [] and api.get(f"/api/v1/editions/{slug}").status_code == 404
    assert api.get("/api/v1/editions/current").status_code == 404


# ------------------------------------------------------------------------------------ H: approval workflow
def test_full_state_machine_with_reapproval_of_the_right_revision(api, db_session):
    """E2E-H-01: generated -> review -> edit -> save -> approve -> edit again (approval invalidated) -> re-review ->
    approve revision 3 -> publish; publishing with the outdated approval or revision is refused at each step."""
    d1 = get(api).json()
    assert d1["status"] == "review_required" and d1["currentRevision"] == 1
    r = api.put(
        f"/api/v1/admin/editions/{api.eid}/content",
        json=payload(d1, introduction=d1["content"]["introduction"] + " Edited."),
        headers=AUTH,
    )
    assert r.status_code == 200
    d2 = get(api).json()
    assert d2["currentRevision"] == 2 and d2["status"] == "review_required"
    # Approving with the hash of revision 1 is refused: the reviewer did not see revision 2.
    assert post(api, "approve", {"expectedRevision": 2, "contentHash": d1["contentHash"]}).status_code == 409
    assert post(api, "approve", {"expectedRevision": 2, "contentHash": d2["contentHash"]}).status_code == 200
    e = db_session.get(Edition, uuid.UUID(api.eid))
    db_session.refresh(e)
    assert e.status == "approved" and e.approved_revision == 2 and e.approved_content_hash == d2["contentHash"]
    # Modify the approved edition: approval is invalidated.
    d2b = get(api).json()
    r = api.put(
        f"/api/v1/admin/editions/{api.eid}/content", json=payload(d2b, title="Changed after approval"), headers=AUTH
    )
    assert r.status_code == 200 and r.json()["status"] == "review_required"
    db_session.refresh(e)
    assert e.approved_revision is None and e.approved_content_hash is None and e.approved_by is None
    # Publishing now (old revision, or current revision without approval) is refused.
    assert post(api, "publish", {"expectedRevision": 2}).status_code == 409
    assert post(api, "publish", {"expectedRevision": 3}).status_code == 409
    d3 = get(api).json()
    assert (
        post(api, "approve", {"expectedRevision": 3, "contentHash": d3["contentHash"]}, token=OTHER).status_code == 200
    )
    pub = post(api, "publish", {"expectedRevision": 3})
    assert pub.status_code == 200 and pub.json()["revision"] == 3
    actions = [
        a.action
        for a in db_session.scalars(
            select(EditionAuditEntry)
            .where(EditionAuditEntry.edition_id == uuid.UUID(api.eid))
            .order_by(EditionAuditEntry.id)
        )
    ]
    assert "approval_invalidated" in actions and actions.count("approved") == 2 and actions[-1] == "published"
    public = api.get("/api/v1/editions/current").json()
    assert public["title"] == "Changed after approval"


def test_approval_from_every_state(api, db_session):
    """E2E-H-02: approval of a draft, a rejected edition, a published edition, with a wrong revision, or with
    validation errors is refused; re-approving an approved edition with the same hash is a harmless no-op."""
    d = get(api).json()
    h = d["contentHash"]
    assert post(api, "approve", {"expectedRevision": 7, "contentHash": h}).status_code == 409  # wrong revision
    assert post(api, "approve", {"expectedRevision": 1, "contentHash": h}).status_code == 200
    again = post(api, "approve", {"expectedRevision": 1, "contentHash": h})
    assert again.status_code == 200 and again.json().get("alreadyApproved") is True
    # Back to review by editing, then reject.
    r = api.put(f"/api/v1/admin/editions/{api.eid}/content", json=payload(get(api).json(), title="Rev 2"), headers=AUTH)
    assert r.status_code == 200
    d2 = get(api).json()
    assert post(api, "reject", {"expectedRevision": 2, "reason": "Needs a better intro"}).status_code == 200
    assert (
        post(api, "approve", {"expectedRevision": 2, "contentHash": d2["contentHash"]}).status_code == 409
    )  # rejected
    # Edit a rejected edition: it becomes a draft, which cannot be approved before submission.
    r = api.put(f"/api/v1/admin/editions/{api.eid}/content", json=payload(get(api).json(), title="Rev 3"), headers=AUTH)
    assert r.json()["status"] == "draft"
    d3 = get(api).json()
    assert post(api, "approve", {"expectedRevision": 3, "contentHash": d3["contentHash"]}).status_code == 409  # draft
    # Invalid content (empty title) cannot be submitted or approved.
    r = api.put(f"/api/v1/admin/editions/{api.eid}/content", json=payload(d3, title=""), headers=AUTH)
    d4 = get(api).json()
    assert d4["status"] == "draft"
    assert post(api, "submit", {"expectedRevision": d4["currentRevision"]}).status_code == 422
    # Valid again, submitted, approved, published: then approval is refused (final state).
    r = api.put(f"/api/v1/admin/editions/{api.eid}/content", json=payload(d4, title="Final title"), headers=AUTH)
    d5 = get(api).json()
    assert post(api, "submit", {"expectedRevision": d5["currentRevision"]}).status_code == 200
    assert (
        post(api, "approve", {"expectedRevision": d5["currentRevision"], "contentHash": d5["contentHash"]}).status_code
        == 200
    )
    assert post(api, "publish", {"expectedRevision": d5["currentRevision"]}).status_code == 200
    late = post(api, "approve", {"expectedRevision": d5["currentRevision"], "contentHash": d5["contentHash"]})
    assert late.status_code == 409  # published is final: no approval action applies
    after = db_session.get(Edition, uuid.UUID(api.eid))
    db_session.refresh(after)
    assert after.status == "published" and after.published_revision == d5["currentRevision"]


# ------------------------------------------------------------------------------------------- I: publication
def test_publication_rules(api, db_session):
    """E2E-I-01: unapproved and unauthorised publication is refused; an approved edition publishes once; repeated
    requests return alreadyPublished and create no second set of public stories; the published snapshot equals the
    approved revision and is read-only."""
    d = get(api).json()
    assert post(api, "publish", {"expectedRevision": 1}).status_code == 409  # not approved
    assert post(api, "approve", {"expectedRevision": 1, "contentHash": d["contentHash"]}).status_code == 200
    assert post(api, "publish", {"expectedRevision": 1}, token=STRANGER).status_code == 403
    assert post(api, "publish", {"expectedRevision": 1}, token=EXPIRED).status_code == 401
    first = post(api, "publish", {"expectedRevision": 1})
    assert first.status_code == 200
    for _ in range(3):
        assert post(api, "publish", {"expectedRevision": 1}).json()["alreadyPublished"] is True
    eid = uuid.UUID(api.eid)
    stories = db_session.scalars(select(Story).where(Story.edition_id == eid)).all()
    assert len(stories) == len(d["content"]["stories"])
    public = api.get("/api/v1/editions/current").json()
    assert [s["sourceUrl"] for s in public["stories"]] == [s["source_url"] for s in d["content"]["stories"]]
    assert [s["headline"] for s in public["stories"]] == [s["text"]["headline"] for s in d["content"]["stories"]]
    # Read-only after publication.
    r = api.put(
        f"/api/v1/admin/editions/{api.eid}/content", json=payload(get(api).json(), title="Late change"), headers=AUTH
    )
    assert r.status_code == 409
    archived = post(api, "archive", {"expectedRevision": 1})
    assert archived.status_code == 409 and archived.json()["error"]["code"] == "invalid_state"
    assert api.get("/api/v1/editions/current").json() == public


def test_database_failure_during_publication_leaves_nothing_half_published(api, db_session, monkeypatch):
    """E2E-I-02: the database fails after the first public story row is written; the whole publication rolls back
    (no public stories, edition still approved and unpublished, failure audited) and a retry then succeeds."""
    from sqlalchemy.exc import OperationalError

    d = get(api).json()
    post(api, "approve", {"expectedRevision": 1, "contentHash": d["contentHash"]})
    service = api.service
    original = service._materialise

    def failing(s, edition, content, now):
        original(s, edition, content.model_copy(update={"stories": content.stories[:1]}), now)
        raise OperationalError("INSERT INTO stories", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(service, "_materialise", failing)
    r = post(api, "publish", {"expectedRevision": 1})
    assert r.status_code == 500 and r.json()["error"]["code"] == "publish_failed"
    assert "server closed" not in r.text  # no internals leak
    eid = uuid.UUID(api.eid)
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Story).where(Story.edition_id == eid)) == 0
    e = db_session.get(Edition, eid)
    assert e.status == "approved" and e.published_at is None
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(EditionAuditEntry)
            .where(EditionAuditEntry.edition_id == eid, EditionAuditEntry.action == "publish_failed")
        )
        == 1
    )
    assert api.get("/api/v1/editions/current").status_code == 404
    monkeypatch.setattr(service, "_materialise", original)
    assert post(api, "publish", {"expectedRevision": 1}).status_code == 200
    assert db_session.scalar(select(func.count()).select_from(Story).where(Story.edition_id == eid)) == len(
        d["content"]["stories"]
    )


def test_selection_rows_link_published_stories(api, db_session):
    """E2E-I-03: after publication each edition item links to its public story; every public story has a source and
    the original URL; selection rows of the edition are kept."""
    d = get(api).json()
    post(api, "approve", {"expectedRevision": 1, "contentHash": d["contentHash"]})
    post(api, "publish", {"expectedRevision": 1})
    eid = uuid.UUID(api.eid)
    db_session.expire_all()
    items = db_session.scalars(select(EditionItem).where(EditionItem.edition_id == eid)).all()
    assert items and all(i.story_id for i in items)
    for st in db_session.scalars(select(Story).where(Story.edition_id == eid)):
        assert st.source_id and st.original_url.startswith("https://") and st.status == "published"
