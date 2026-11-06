"""Admin processing endpoints: off by default, bearer-token protected, never public. No database needed."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.processing.runner import ProcessingRunner
from app.processing.store import MemoryProcessingStore
from tests.conftest import make_settings
from tests.processing_helpers import NOW, SOURCES, staged

TOKEN = "p" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ITEMS = [
    staged("Example Labs releases Nimbus 2.5", "https://labs.example.com/nimbus", source="lab-blog"),
    staged("Example Labs cuts Nimbus 2.5 API prices", "https://news.example.com/pricing"),
]


def make(token: str | None = TOKEN):
    settings = make_settings(database_url=None, ingestion_admin_token=token)
    app = create_app(settings)
    store = MemoryProcessingStore(ITEMS)
    app.state.processing_runner_factory = lambda s, db: ProcessingRunner(s, store, sources=SOURCES, clock=lambda: NOW)
    return TestClient(app), store


def test_endpoints_do_not_exist_without_a_token():
    client, store = make(token=None)
    assert client.post("/api/v1/admin/processing/runs", headers=AUTH).status_code == 404
    assert client.get("/api/v1/admin/processing/clusters", headers=AUTH).status_code == 404
    assert store.jobs == {}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
def test_bad_credentials_are_rejected(headers):
    client, store = make()
    assert client.post("/api/v1/admin/processing/runs", headers=headers).status_code == 401
    assert client.get("/api/v1/admin/processing/clusters", headers=headers).status_code == 401
    assert store.jobs == {}


def test_run_then_read_clusters():
    client, _ = make()
    response = client.post("/api/v1/admin/processing/runs", headers=AUTH)
    assert response.status_code == 200 and response.json()["counts"]["processed"] == 2
    clusters = client.get("/api/v1/admin/processing/clusters?limit=10", headers=AUTH).json()
    assert len(clusters) == 2 and {c["eventType"] for c in clusters} == {"launch", "pricing"}
    assert all(c["contentIsUntrusted"] and c["originalSource"] is None for c in clusters)
    assert client.get("/api/v1/admin/processing/clusters?limit=0", headers=AUTH).status_code == 422


def test_admin_routes_are_not_public():
    client, _ = make()
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("processing" in p for p in paths)
    preflight = client.options(
        "/api/v1/admin/processing/runs",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert "POST" not in preflight.headers.get("access-control-allow-methods", "")
