"""Stage 3.6 admin API and security: authentication, authorisation, no public or arbitrary deletion endpoint, id
validation, rate limiting, RLS and the protected audit history. Rolled back; no network."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.main import create_app
from app.models import CleanupJob, StoryAnalysisRecord
from tests.cleanup_helpers import AFTER_HOLD, LATER, add_working_data, exists, make_cleanup, published_edition
from tests.conftest import make_settings
from tests.edition_helpers import SavepointDatabase
from tests.test_edition_admin_api import AUTH, INGESTION_TOKEN, OTHER_TOKEN, STRANGER, FakeVerifier

pytestmark = pytest.mark.usefixtures("engine")


@pytest.fixture
def api(db_session):
    eid, selected, run, _ = published_edition(db_session)
    extra = uuid.uuid4()
    add_working_data(db_session, extra)
    service = make_cleanup(db_session)
    settings = make_settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        ingestion_admin_token=INGESTION_TOKEN,
        admin_write_rate_per_minute=50,
    )
    app = create_app(settings, database=SavepointDatabase(settings, db_session))
    app.state.admin_token_verifier = FakeVerifier()
    app.state.cleanup_service_factory = lambda s, db: service
    client = TestClient(app)
    client.service, client.session, client.edition_id, client.extra = service, db_session, str(eid), extra
    return client


def test_cleanup_api_is_absent_without_admin_sign_in(db_session):
    settings = make_settings()
    client = TestClient(create_app(settings, database=SavepointDatabase(settings, db_session)))
    for method, path in (("get", "/jobs"), ("get", "/policy"), ("post", "/run")):
        assert getattr(client, method)(f"/api/v1/admin/cleanup{path}", headers=AUTH).status_code == 404


def test_authentication_and_authorisation(api):
    jid = str(uuid.uuid4())
    calls = [
        ("get", "/api/v1/admin/cleanup/jobs"),
        ("get", "/api/v1/admin/cleanup/policy"),
        ("get", f"/api/v1/admin/cleanup/jobs/{jid}"),
        ("post", f"/api/v1/admin/cleanup/jobs/{jid}/retry"),
        ("post", f"/api/v1/admin/cleanup/jobs/{jid}/cancel"),
        ("post", f"/api/v1/admin/cleanup/editions/{api.edition_id}/plan"),
        ("post", "/api/v1/admin/cleanup/run"),
    ]
    for method, path in calls:
        assert getattr(api, method)(path).status_code == 401, path
        assert getattr(api, method)(path, headers={"Authorization": f"Bearer {INGESTION_TOKEN}"}).status_code == 401
        assert getattr(api, method)(path, headers={"Authorization": f"Bearer {STRANGER}"}).status_code == 403, path
    assert api.session.scalar(select(CleanupJob.id)) is None  # nothing happened


def test_admin_flow_over_http(api):
    api.service.test_clock.now = LATER
    out = api.post(f"/api/v1/admin/cleanup/editions/{api.edition_id}/plan", headers=AUTH)
    assert out.status_code == 200, out.text
    job = out.json()
    assert job["status"] == "staged" and job["eligible"] >= 1
    assert out.headers["cache-control"] == "no-store"
    listing = api.get("/api/v1/admin/cleanup/jobs", headers=AUTH).json()["items"]
    assert listing[0]["id"] == job["id"] and listing[0]["editionSlug"] == "2026-w45"
    detail = api.get(f"/api/v1/admin/cleanup/jobs/{job['id']}?state=protected", headers=AUTH).json()
    assert detail["items"] and all(i["state"] == "protected" for i in detail["items"])
    assert detail["audit"][-1]["action"] == "job_created"
    assert api.get("/api/v1/admin/cleanup/policy", headers=AUTH).json()["version"] == api.service.config.version
    # Running now before the manifest hold deletes nothing: the manual run follows the same rules.
    assert api.post("/api/v1/admin/cleanup/run", headers=AUTH).json()["deleted"] == 0
    assert exists(api.session, StoryAnalysisRecord, api.extra)
    cancel = api.post(
        f"/api/v1/admin/cleanup/jobs/{job['id']}/cancel", headers={"Authorization": f"Bearer {OTHER_TOKEN}"}
    )
    assert cancel.json()["status"] == "cancelled"
    reopen = api.post(f"/api/v1/admin/cleanup/jobs/{job['id']}/reopen", headers=AUTH)
    assert reopen.json()["status"] == "staged"
    api.service.test_clock.now = AFTER_HOLD + timedelta_hours(25)
    assert api.post("/api/v1/admin/cleanup/run", headers=AUTH).json()["deleted"] >= 1
    assert not exists(api.session, StoryAnalysisRecord, api.extra)
    retry = api.post(f"/api/v1/admin/cleanup/jobs/{job['id']}/retry", headers=AUTH)
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "invalid_state"


def timedelta_hours(n):
    from datetime import timedelta

    return timedelta(hours=n)


def test_invalid_ids_and_filters_are_rejected(api):
    for path in (
        "/api/v1/admin/cleanup/jobs/not-a-uuid",
        "/api/v1/admin/cleanup/jobs/1;DROP TABLE editions/cancel",
        "/api/v1/admin/cleanup/editions/editions/plan",
    ):
        r = api.post(path, headers=AUTH) if path.endswith(("cancel", "plan")) else api.get(path, headers=AUTH)
        assert r.status_code in (404, 422), path
    assert api.get(f"/api/v1/admin/cleanup/jobs/{uuid.uuid4()}?state=everything", headers=AUTH).status_code == 422
    assert api.get(f"/api/v1/admin/cleanup/jobs/{uuid.uuid4()}", headers=AUTH).status_code == 404
    assert api.post(f"/api/v1/admin/cleanup/editions/{uuid.uuid4()}/plan", headers=AUTH).status_code == 404


def test_no_endpoint_deletes_a_chosen_record_or_table(api):
    routes = {(m, r.path) for r in api.app.routes for m in getattr(r, "methods", ())}
    cleanup = {(m, p) for m, p in routes if "/cleanup" in p}
    assert not any(m == "DELETE" for m, _ in cleanup)
    assert all(p.startswith("/api/v1/admin/") for _, p in cleanup)
    # Unknown body fields (a table name, raw SQL, a status) are never read.
    r = api.post("/api/v1/admin/cleanup/run", headers=AUTH, json={"table": "editions", "sql": "DELETE FROM editions"})
    assert r.status_code == 200
    assert api.session.scalar(text("SELECT count(*) FROM editions")) == 1
    schema = api.get("/openapi.json")
    assert schema.status_code != 200 or "/cleanup" not in schema.text


def test_manual_run_is_rate_limited(api):
    api.service.config = api.service.config.model_copy(
        update={"execution": api.service.config.execution.model_copy(update={"manual_runs_per_hour": 1})}
    )
    assert api.post("/api/v1/admin/cleanup/run", headers=AUTH).status_code == 200
    r = api.post("/api/v1/admin/cleanup/run", headers=AUTH)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


def test_cleanup_tables_have_rls_no_api_access_and_cannot_be_rewritten(api):
    session = api.session
    api.service.test_clock.now = LATER
    api.service.scheduled_run()
    for table in ("cleanup_jobs", "cleanup_manifest_items", "cleanup_audit_log"):
        assert session.scalar(text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": table})
    for sql in (
        "DELETE FROM cleanup_audit_log",
        "UPDATE cleanup_audit_log SET action = 'job_created'",
        "TRUNCATE cleanup_audit_log",
        "DELETE FROM cleanup_jobs",
        "TRUNCATE cleanup_jobs CASCADE",
        "DELETE FROM cleanup_manifest_items",
    ):
        with pytest.raises(DBAPIError, match="append-only|not allowed"):
            with session.begin_nested():
                session.execute(text(sql))
    with session.begin_nested():
        session.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') "
                "THEN CREATE ROLE anon NOLOGIN; END IF; END $$"
            )
        )
        session.execute(text("GRANT USAGE ON SCHEMA public TO anon"))
        session.execute(text("SET LOCAL ROLE anon"))
        for table in ("cleanup_jobs", "cleanup_manifest_items", "cleanup_audit_log"):
            with pytest.raises(DBAPIError, match="permission denied"):
                with session.begin_nested():
                    session.execute(text(f"SELECT * FROM {table}"))
        session.execute(text("RESET ROLE"))


def test_error_responses_do_not_leak_internals(api, monkeypatch):
    api.service.test_clock.now = LATER
    job = api.post(f"/api/v1/admin/cleanup/editions/{api.edition_id}/plan", headers=AUTH).json()
    monkeypatch.setattr(api.service, "_delete_in_tx", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pw=secret")))
    api.service.test_clock.now = AFTER_HOLD + timedelta_hours(1)
    body = api.post("/api/v1/admin/cleanup/run", headers=AUTH).json()
    assert body["status"] == "failed" and "secret" not in str(body)
    detail = api.get(f"/api/v1/admin/cleanup/jobs/{job['id']}", headers=AUTH).json()
    assert "secret" not in str(detail) and detail["lastError"] == "RuntimeError during deletion."
