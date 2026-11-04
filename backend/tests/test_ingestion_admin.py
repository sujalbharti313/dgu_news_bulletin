"""Admin ingestion endpoints: off by default, bearer-token protected, bounded inputs. No database needed."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ingestion.runner import IngestionRunner
from app.ingestion.sources import RssSourceConfig
from app.ingestion.store import MemoryIngestionStore
from app.main import create_app
from tests.conftest import make_settings
from tests.ingestion_helpers import fixture, make_http

TOKEN = "t" * 40
SOURCES = [RssSourceConfig(key="good", type="rss", name="Good", category="news", url="https://good.example.com/rss")]


def make(token: str | None = TOKEN, store=None):
    settings = make_settings(database_url=None, ingestion_admin_token=token)
    app = create_app(settings)
    store = store if store is not None else MemoryIngestionStore()

    def factory(s, db):
        return IngestionRunner(
            s,
            store,
            sources=SOURCES,
            http_factory=lambda: make_http(lambda r: httpx.Response(200, content=fixture("rss2.xml"))),
        )

    app.state.ingestion_runner_factory = factory
    return TestClient(app), store


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_endpoints_do_not_exist_without_a_token():
    client, _ = make(token=None)
    assert client.post("/api/v1/admin/ingestion/runs", json={}, headers=AUTH).status_code == 404
    assert client.get("/api/v1/admin/ingestion/status", headers=AUTH).status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
    ],
)
def test_bad_credentials_are_rejected(headers):
    client, store = make()
    response = client.post("/api/v1/admin/ingestion/runs", json={"wait": True}, headers=headers)
    assert response.status_code == 401 and response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"
    assert store.jobs == {}


def test_short_admin_token_is_refused_at_startup():
    with pytest.raises(ValueError, match="at least 32"):
        make_settings(ingestion_admin_token="short")


def test_run_and_wait_returns_report():
    client, store = make()
    response = client.post("/api/v1/admin/ingestion/runs", json={"wait": True}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded" and body["sources"][0]["counts"]["inserted"] == 2
    assert len(store.items) == 2


def test_background_run_is_accepted():
    client, store = make()
    response = client.post("/api/v1/admin/ingestion/runs", json={"sources": ["good"]}, headers=AUTH)
    assert response.status_code == 202 and response.json() == {"status": "accepted", "sources": ["good"]}
    assert len(store.items) == 2  # TestClient runs background tasks before returning


def test_only_configured_sources_can_be_requested():
    client, _ = make()
    unknown = client.post("/api/v1/admin/ingestion/runs", json={"sources": ["evil"]}, headers=AUTH)
    assert unknown.status_code == 400
    url = client.post("/api/v1/admin/ingestion/runs", json={"url": "http://169.254.169.254/"}, headers=AUTH)
    assert url.status_code == 422
    too_many = client.post("/api/v1/admin/ingestion/runs", json={"sources": ["good"] * 51}, headers=AUTH)
    assert too_many.status_code == 422


def test_status_reports_per_source_state():
    client, _ = make()
    client.post("/api/v1/admin/ingestion/runs", json={"wait": True}, headers=AUTH)
    body = client.get("/api/v1/admin/ingestion/status", headers=AUTH).json()
    (source,) = body["sources"]
    assert source["key"] == "good" and source["lastStatus"] == "succeeded"
    assert source["pendingItems"] == 2 and source["consecutiveFailures"] == 0
    assert body["runningInThisProcess"] is False


def test_admin_routes_are_not_in_the_public_schema_and_cors_stays_get_only():
    client, _ = make()
    assert "/api/v1/admin/ingestion/runs" not in client.get("/openapi.json").json()["paths"]
    preflight = client.options(
        "/api/v1/admin/ingestion/runs",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert "POST" not in preflight.headers.get("access-control-allow-methods", "")


def test_database_not_configured_returns_503():
    settings = make_settings(database_url=None, ingestion_admin_token=TOKEN)
    client = TestClient(create_app(settings))
    response = client.get("/api/v1/admin/ingestion/status", headers=AUTH)
    assert response.status_code == 503
