"""Admin ranking endpoints: off by default, bearer-token protected, read-only for scores, never public."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.intelligence_helpers import FAKE_KEY, INDIA, PAPER, POLICY
from tests.ranking_helpers import analysed, make_ranker, ranking_settings

TOKEN = "r" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROUTES = [("post", "/api/v1/admin/ranking/runs"), ("get", "/api/v1/admin/ranking/runs"),
          ("get", "/api/v1/admin/ranking/runs/latest"), ("get", "/api/v1/admin/ranking/verifications")]  # fmt: skip


def make(token: str | None = TOKEN):
    settings = ranking_settings(ingestion_admin_token=token)
    app = create_app(settings)
    clusters, records = analysed([PAPER, INDIA, POLICY])
    runner, store = make_ranker(clusters, records, settings=settings)
    app.state.ranking_runner_factory = lambda s, db: runner
    return TestClient(app), store


def test_endpoints_do_not_exist_without_a_token():
    client, store = make(token=None)
    for method, path in ROUTES:
        assert getattr(client, method)(path, headers=AUTH).status_code == 404
    assert store.runs == {}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
def test_bad_credentials_are_rejected(headers):
    client, store = make()
    for method, path in ROUTES:
        assert getattr(client, method)(path, headers=headers).status_code == 401
    assert store.runs == {}


def test_run_then_read_latest_runs_and_verifications():
    client, _ = make()
    assert client.get("/api/v1/admin/ranking/runs/latest", headers=AUTH).status_code == 404
    run = client.post("/api/v1/admin/ranking/runs?week=current", headers=AUTH)
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "completed" and body["result"]["edition"]["startDate"] == "2026-11-02"
    latest = client.get("/api/v1/admin/ranking/runs/latest", headers=AUTH).json()
    assert latest["rankedCandidateIds"] == body["result"]["rankedCandidateIds"]
    assert {"finalScores", "rankingExplanation", "eligibilityStatus"} <= set(latest["candidates"][0])
    runs = client.get("/api/v1/admin/ranking/runs", headers=AUTH).json()
    assert len(runs) == 1 and runs[0]["status"] == "completed"
    rows = client.get("/api/v1/admin/ranking/verifications?eligibility=eligible", headers=AUTH).json()
    assert rows and all(r["eligibility"] == "eligible" for r in rows)
    text = run.text + str(latest) + str(rows)
    assert FAKE_KEY not in text and "SECURITY RULES" not in text


def test_scores_cannot_be_changed_through_the_api():
    client, _ = make()
    for method in ("put", "patch", "delete"):
        for path in ("/api/v1/admin/ranking/runs", "/api/v1/admin/ranking/verifications"):
            assert getattr(client, method)(path, headers=AUTH).status_code in (404, 405)


def test_week_parameter_is_validated():
    client, store = make()
    assert client.post("/api/v1/admin/ranking/runs?week=drop table", headers=AUTH).status_code == 422
    assert store.runs == {}


def test_routes_are_not_in_the_public_schema():
    client, _ = make()
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("ranking" in p for p in paths)
