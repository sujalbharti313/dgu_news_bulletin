"""Stage 3.5 admin API: authentication (Supabase token verification + editorial_admins allow-list), authorisation,
no client-controlled status, the full review → approve → publish flow over HTTP, rate limiting, caching headers,
CORS, and that the public API never exposes unpublished editions."""

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.editions.auth import AuthUnavailable, SupabaseTokenVerifier, VerifiedUser
from app.main import create_app
from app.models import Edition, EditorialAdmin
from tests.conftest import make_settings
from tests.edition_helpers import ADMIN_ID, OTHER_ADMIN_ID, SavepointDatabase, make_service, seed_admins, seed_pipeline

pytestmark = pytest.mark.usefixtures("engine")
TOKEN = "header.admin-token.signature"
OTHER_TOKEN = "header.second-admin.signature"
STRANGER = "header.signed-in-stranger.signature"
INGESTION_TOKEN = "i" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeVerifier:
    def __init__(self):
        self.users = {
            TOKEN: VerifiedUser(ADMIN_ID, "editor@example.com"),
            OTHER_TOKEN: VerifiedUser(OTHER_ADMIN_ID, "second@example.com"),
            STRANGER: VerifiedUser(uuid.uuid4(), "student@example.com"),
        }
        self.down = False

    def verify(self, token):
        if self.down:
            raise AuthUnavailable()
        return self.users.get(token)


@pytest.fixture
def api(db_session):
    seed_admins(db_session)
    seed_pipeline(db_session)
    service = make_service(db_session)
    settings = make_settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        ingestion_admin_token=INGESTION_TOKEN,
        cors_origins="https://bulletin.example.edu",
        admin_write_rate_per_minute=50,
    )
    app = create_app(settings, database=SavepointDatabase(settings, db_session))
    app.state.admin_token_verifier = FakeVerifier()
    app.state.edition_service_factory = lambda s, db: service
    client = TestClient(app)
    report = service.generate_week(service.clock().date().replace(day=2), actor=service_actor())
    client.edition_id = str(report.edition_id)
    client.session = db_session
    return client


def service_actor():
    from app.editions.service import Actor

    return Actor.scheduler()


def test_admin_api_is_absent_without_supabase_auth_configured(db_session):
    settings = make_settings()
    client = TestClient(create_app(settings, database=SavepointDatabase(settings, db_session)))
    assert client.get("/api/v1/admin/editions", headers=AUTH).status_code == 404


def test_authentication_and_authorisation(api):
    eid = api.edition_id
    assert api.get("/api/v1/admin/editions").status_code == 401
    assert api.get("/api/v1/admin/editions", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.get("/api/v1/admin/editions", headers={"Authorization": f"Bearer {INGESTION_TOKEN}"}).status_code == 401
    stranger = {"Authorization": f"Bearer {STRANGER}"}
    assert api.get("/api/v1/admin/editions", headers=stranger).status_code == 403
    assert (
        api.post(
            f"/api/v1/admin/editions/{eid}/approve",
            json={"expectedRevision": 1, "contentHash": "0" * 64},
            headers=stranger,
        ).status_code
        == 403
    )
    assert api.post(f"/api/v1/admin/editions/{eid}/publish", json={"expectedRevision": 1}).status_code == 401
    me = api.get("/api/v1/admin/editions/session", headers=AUTH)
    assert me.status_code == 200 and me.json()["email"] == "editor@example.com"
    assert me.headers["cache-control"] == "no-store"


def test_deactivated_admin_loses_access_immediately(api):
    admin = api.session.get(EditorialAdmin, ADMIN_ID)
    admin.is_active = False
    api.session.flush()
    assert api.get("/api/v1/admin/editions", headers=AUTH).status_code == 403


def test_auth_service_outage_is_a_safe_503(api):
    api.app.state.admin_token_verifier.down = True
    r = api.get("/api/v1/admin/editions", headers=AUTH)
    assert r.status_code == 503 and "token" not in r.text.lower()


def test_full_review_approve_publish_flow_over_http(api):
    eid = api.edition_id
    listing = api.get("/api/v1/admin/editions", headers=AUTH).json()["items"]
    assert listing[0]["id"] == eid and listing[0]["status"] == "review_required"
    detail = api.get(f"/api/v1/admin/editions/{eid}", headers=AUTH).json()
    assert "approve" in detail["allowedActions"] and detail["contentHash"]
    preview = api.get(f"/api/v1/admin/editions/{eid}/preview", headers=AUTH).json()
    assert preview["preview"] is True and preview["stories"][0]["sourceUrl"].startswith("http")
    # Publishing an unapproved edition is refused.
    r = api.post(f"/api/v1/admin/editions/{eid}/publish", json={"expectedRevision": 1}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"
    r = api.post(
        f"/api/v1/admin/editions/{eid}/approve",
        json={"expectedRevision": 1, "contentHash": detail["contentHash"]},
        headers=AUTH,
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert api.get("/api/v1/editions/current").status_code == 404  # still not public
    r = api.post(
        f"/api/v1/admin/editions/{eid}/publish",
        json={"expectedRevision": 1},
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )
    assert r.status_code == 200 and r.json()["status"] == "published"
    again = api.post(f"/api/v1/admin/editions/{eid}/publish", json={"expectedRevision": 1}, headers=AUTH)
    assert again.status_code == 200 and again.json()["alreadyPublished"] is True
    public = api.get("/api/v1/editions/current").json()
    assert public["introduction"] == detail["content"]["introduction"] and public["stories"]
    final = api.get(f"/api/v1/admin/editions/{eid}", headers=AUTH).json()
    assert final["approval"]["by"]["email"] == "editor@example.com"
    assert final["publication"]["by"]["email"] == "second@example.com"
    assert {"approved", "published", "opened_for_review"} <= {a["action"] for a in final["audit"]}


def test_client_cannot_set_status_or_derived_fields(api):
    eid = api.edition_id
    detail = api.get(f"/api/v1/admin/editions/{eid}", headers=AUTH).json()
    st = detail["content"]["stories"][0]
    body = {
        "expectedRevision": 1,
        "status": "published",
        "title": "A title edited by the admin",
        "introduction": detail["content"]["introduction"],
        "editorialNote": "",
        "stories": [
            {
                "clusterId": st["cluster_id"],
                **_camel(st["text"]),
                "sourceUrl": "https://evil.example/x",
                "publisher": "Evil",
            }
        ],
    }
    r = api.put(f"/api/v1/admin/editions/{eid}/content", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    after = api.get(f"/api/v1/admin/editions/{eid}", headers=AUTH).json()
    assert after["status"] == "review_required"
    assert after["content"]["stories"][0]["source_url"] == st["source_url"]
    assert after["content"]["stories"][0]["publisher"] == st["publisher"]
    edition = api.session.get(Edition, uuid.UUID(eid))
    assert edition.published_at is None


def _camel(text):
    return {
        "headline": text["headline"],
        "summary": text["summary"],
        "technicalExplanation": text["technical_explanation"],
        "whyItMatters": text["why_it_matters"],
        "studentRelevance": text["student_relevance"],
        "projectIdea": text["project_idea"],
    }


def test_invalid_input_is_rejected_safely(api):
    eid = api.edition_id
    assert api.get("/api/v1/admin/editions/not-a-uuid", headers=AUTH).status_code == 422
    assert api.get(f"/api/v1/admin/editions/{uuid.uuid4()}", headers=AUTH).status_code == 404
    r = api.post(
        f"/api/v1/admin/editions/{eid}/approve",
        json={"expectedRevision": 1, "contentHash": "' OR 1=1 --"},
        headers=AUTH,
    )
    assert r.status_code == 422 and "OR 1=1" not in r.text
    r = api.post(f"/api/v1/admin/editions/{eid}/reject", json={"expectedRevision": 9, "reason": "Bad"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_revision"
    r = api.post("/api/v1/admin/editions/generate", json={"week": "2099-01-05"}, headers=AUTH)
    assert r.status_code == 422


def test_write_rate_limit(api):
    api.app.state.admin_write_limiter = None
    api.app.state.settings = api.app.state.settings.model_copy(update={"admin_write_rate_per_minute": 2})
    codes = [
        api.post(
            f"/api/v1/admin/editions/{api.edition_id}/submit", json={"expectedRevision": 1}, headers=AUTH
        ).status_code
        for _ in range(3)
    ]
    assert codes[-1] == 429


@pytest.mark.parametrize("path", ["/api/v1/admin/editions", "/api/v1/admin/cleanup/jobs/x/retry"])
def test_cors_allows_only_the_frontend_origin_with_authorization(api, path):
    # The /admin/editions and /admin/cleanup pages both call the API cross-origin with a bearer token.
    ok = api.options(
        path,
        headers={
            "Origin": "https://bulletin.example.edu",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://bulletin.example.edu"
    assert "access-control-allow-credentials" not in ok.headers
    bad = api.options(
        "/api/v1/admin/editions",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in bad.headers


@pytest.mark.parametrize("path", ["/api/v1/admin/ingestion/runs", "/api/v1/admin/editionsx", "/api/v1/stories"])
def test_cors_gives_other_paths_no_authorization_header(api, path):
    response = api.options(
        path,
        headers={
            "Origin": "https://bulletin.example.edu",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 400


def test_public_api_hides_every_unpublished_state(api):
    session = api.session
    edition = session.scalar(select(Edition).where(Edition.id == uuid.UUID(api.edition_id)))
    for status in ("generating", "draft", "review_required", "generation_failed", "rejected", "archived"):
        session.execute(Edition.__table__.update().where(Edition.id == edition.id).values(status=status))
        session.flush()
        assert api.get("/api/v1/editions/current").status_code == 404, status
        assert api.get(f"/api/v1/editions/{edition.slug}").status_code == 404, status
        assert api.get("/api/v1/editions").json()["total"] == 0, status


def test_supabase_verifier_checks_tokens_with_supabase_and_caches():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        assert request.headers["apikey"] == "sb_publishable_test"
        if request.headers["authorization"] == f"Bearer {TOKEN}":
            return httpx.Response(200, json={"id": str(ADMIN_ID), "email": "editor@example.com"})
        if request.headers["authorization"] == "Bearer a.down.token":
            return httpx.Response(500)
        return httpx.Response(401, json={"msg": "invalid JWT"})

    settings = make_settings(supabase_url="https://project.supabase.co", supabase_publishable_key="sb_publishable_test")
    verifier = SupabaseTokenVerifier(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert verifier.verify(TOKEN).user_id == ADMIN_ID
    assert verifier.verify(TOKEN).user_id == ADMIN_ID
    assert len(calls) == 1 and str(calls[0].url) == "https://project.supabase.co/auth/v1/user"
    assert verifier.verify("x.bad.token") is None
    assert verifier.verify("not-a-jwt") is None and len(calls) == 2  # malformed tokens are not even sent
    with pytest.raises(AuthUnavailable):
        verifier.verify("a.down.token")
