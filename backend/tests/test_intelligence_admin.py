"""Admin intelligence endpoints: off by default, bearer-token protected, never public. No database needed."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.intelligence_helpers import (
    FAKE_KEY,
    PAPER,
    POLICY,
    FakeProvider,
    ai_settings,
    clusters,
    grounded,
    make_runner,
)

TOKEN = "i" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROUTES = [("post", "/api/v1/admin/intelligence/runs"), ("get", "/api/v1/admin/intelligence/analyses"),
          ("get", "/api/v1/admin/intelligence/records"), ("get", "/api/v1/admin/intelligence/usage")]  # fmt: skip


def make(token: str | None = TOKEN):
    settings = ai_settings(ingestion_admin_token=token)
    app = create_app(settings)
    provider = FakeProvider(grounded)
    runner, store = make_runner(clusters([PAPER, POLICY]), provider, settings=settings)
    app.state.intelligence_runner_factory = lambda s, db: runner
    return TestClient(app), store, provider


def test_endpoints_do_not_exist_without_a_token():
    client, store, provider = make(token=None)
    for method, path in ROUTES:
        assert getattr(client, method)(path, headers=AUTH).status_code == 404
    assert provider.requests == [] and store.jobs == {}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
def test_bad_credentials_are_rejected(headers):
    client, store, provider = make()
    for method, path in ROUTES:
        assert getattr(client, method)(path, headers=headers).status_code == 401
    assert provider.requests == []


def test_run_then_read_analyses_records_and_usage():
    client, _, provider = make()
    run = client.post("/api/v1/admin/intelligence/runs", headers=AUTH)
    assert run.status_code == 200, run.text
    assert run.json()["counts"]["completed"] == 2
    analyses = client.get("/api/v1/admin/intelligence/analyses", headers=AUTH).json()
    assert len(analyses) == 2 and {"storyClusterId", "priorityAssessment", "primaryCategory"} <= set(analyses[0])
    records = client.get("/api/v1/admin/intelligence/records", headers=AUTH).json()
    assert {r["status"] for r in records} == {"completed"}
    usage = client.get("/api/v1/admin/intelligence/usage", headers=AUTH).json()
    assert usage["requests"] == 2 and usage["apiKeyConfigured"] is True
    body = run.text + str(analyses) + str(records) + str(usage)
    assert FAKE_KEY not in body and "SECURITY RULES" not in body  # no key, no internal prompt


def test_routes_are_not_in_the_public_schema():
    client, *_ = make()
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("intelligence" in p for p in paths)
