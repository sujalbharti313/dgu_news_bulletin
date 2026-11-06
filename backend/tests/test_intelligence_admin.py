"""Admin intelligence endpoints: off by default, bearer-token protected, never public. No database or key."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings
from tests.intelligence_helpers import FakeProvider, ai_settings, clusters, grounded, make_runner

TOKEN = "i" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROUTES = [
    ("post", "/api/v1/admin/intelligence/runs"),
    ("get", "/api/v1/admin/intelligence/stories"),
    ("get", "/api/v1/admin/intelligence/records"),
]


def make(token: str | None = TOKEN, settings=None, provider=None):
    app = create_app(make_settings(database_url=None, ingestion_admin_token=token))
    provider = provider or FakeProvider(grounded)
    runner, store = make_runner(clusters(), provider, settings=settings)
    app.state.intelligence_runner_factory = lambda s, db: runner
    return TestClient(app), store, provider


def test_endpoints_do_not_exist_without_a_token():
    client, store, provider = make(token=None)
    for method, path in ROUTES:
        assert getattr(client, method)(path, headers=AUTH).status_code == 404
    assert store.jobs == {} and provider.requests == []


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
def test_bad_credentials_are_rejected(headers):
    client, store, provider = make()
    for method, path in ROUTES:
        assert getattr(client, method)(path, headers=headers).status_code == 401
    assert provider.requests == []


def test_run_then_read_stories_and_records():
    client, _, provider = make()
    response = client.post("/api/v1/admin/intelligence/runs", headers=AUTH)
    assert response.status_code == 200, response.json()
    assert response.json()["counts"]["new"] == 1 and len(provider.requests) == 1
    stories = client.get("/api/v1/admin/intelligence/stories", headers=AUTH).json()
    assert len(stories) == 1 and stories[0]["contentIsAiGenerated"] is True
    assert stories[0]["sources"][0]["refId"] == "S1"
    records = client.get("/api/v1/admin/intelligence/records", headers=AUTH).json()
    assert records[0]["attemptCount"] == 1 and records[0]["inputTokens"] == 1000
    assert client.get("/api/v1/admin/intelligence/stories?limit=0", headers=AUTH).status_code == 422
    # Re-running is idempotent through the API too.
    again = client.post("/api/v1/admin/intelligence/runs", headers=AUTH).json()
    assert again["counts"]["up_to_date"] == 1 and len(provider.requests) == 1


def test_unconfigured_engine_returns_503_without_calls():
    client, _, provider = make(settings=ai_settings(ai_model=None))
    response = client.post("/api/v1/admin/intelligence/runs", headers=AUTH)
    assert response.status_code == 503 and "AI_MODEL" in response.json()["reason"]
    assert provider.requests == []


def test_admin_routes_are_not_public():
    client, _, _ = make()
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("intelligence" in p for p in paths)
    preflight = client.options(
        "/api/v1/admin/intelligence/runs",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert "POST" not in preflight.headers.get("access-control-allow-methods", "")
