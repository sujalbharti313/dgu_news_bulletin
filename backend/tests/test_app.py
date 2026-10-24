"""Startup, routing, error shape and HTTP-level safeguards. No database needed."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

EXPECTED_ROUTES = {
    "/api/v1/health",
    "/api/v1/health/live",
    "/api/v1/stories",
    "/api/v1/stories/{story_id_or_slug}",
    "/api/v1/search",
    "/api/v1/editions",
    "/api/v1/editions/current",
    "/api/v1/editions/{slug}",
    "/api/v1/briefings/latest",
    "/api/v1/tools",
    "/api/v1/tools/{slug}",
    "/api/v1/research",
    "/api/v1/research/{id_or_slug}",
    "/api/v1/india",
    "/api/v1/career-signals",
    "/api/v1/campus",
}


def offline_app(**overrides):
    settings = Settings(_env_file=None, app_env="test", database_url=None, rate_limit_per_minute=0, **overrides)
    return create_app(settings)


def unreachable_app(**overrides):
    """Configured database that cannot be reached: requests that touch it fail, validation does not."""
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql://secret_user:secret_pw@127.0.0.1:1/aicampus_test",
        db_connect_timeout_seconds=1,
        rate_limit_per_minute=0,
        **overrides,
    )
    return create_app(settings)


def test_app_starts_without_a_database_and_registers_routes():
    with TestClient(offline_app()) as client:
        spec = client.get("/openapi.json").json()
    assert EXPECTED_ROUTES <= set(spec["paths"])


def test_only_read_methods_are_exposed():
    app = offline_app()
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
        assert {method for ops in spec["paths"].values() for method in ops} == {"get"}
        for method in ("post", "put", "patch", "delete"):
            res = getattr(client, method)("/api/v1/stories")
            assert res.status_code == 405
            assert res.json()["error"]["code"] == "method_not_allowed"


def test_liveness_needs_no_database():
    with TestClient(offline_app()) as client:
        res = client.get("/api/v1/health/live")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_health_reports_unconfigured_database_honestly():
    with TestClient(offline_app()) as client:
        res = client.get("/api/v1/health")
    assert res.status_code == 503
    assert res.json() == {"status": "degraded", "version": res.json()["version"], "database": "not_configured"}


def test_data_endpoints_return_503_when_database_is_not_configured():
    with TestClient(offline_app()) as client:
        res = client.get("/api/v1/stories")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "database_unavailable"


def test_unreachable_database_is_503_and_leaks_nothing():
    with TestClient(unreachable_app()) as client:
        for path in ("/api/v1/health", "/api/v1/stories", "/api/v1/editions/current"):
            res = client.get(path)
            assert res.status_code == 503, path
            assert "secret" not in res.text and "127.0.0.1" not in res.text and "Traceback" not in res.text


def test_unknown_route_uses_error_shape():
    with TestClient(offline_app()) as client:
        res = client.get("/api/v1/does-not-exist")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


def test_unhandled_errors_hide_internals():
    app = offline_app()

    @app.get("/boom")
    def boom():
        raise RuntimeError("internal detail that must not leak")

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.get("/boom")
    assert res.status_code == 500
    assert res.json() == {"error": {"code": "internal_error", "message": "An unexpected error occurred."}}


def test_validation_errors_are_structured_without_echoing_input():
    with TestClient(unreachable_app()) as client:
        res = client.get("/api/v1/stories", params={"pageSize": 0, "topic": "<script>"})
    assert res.status_code == 422
    body = res.json()["error"]
    assert body["code"] == "validation_error"
    assert {tuple(d["loc"]) for d in body["details"]} == {("query", "pageSize"), ("query", "topic")}
    assert "<script>" not in res.text


@pytest.mark.parametrize(
    "params",
    [
        {"pageSize": 101},
        {"page": 0},
        {"q": "x" * 201},
        {"sort": "random"},
        {"from": "not-a-date"},
        {"featured": "maybe"},
    ],
)
def test_story_query_bounds(params):
    # 422 (not 503) from an unreachable database proves invalid requests never open a connection.
    with TestClient(unreachable_app()) as client:
        assert client.get("/api/v1/stories", params=params).status_code == 422


def test_search_requires_a_query():
    with TestClient(unreachable_app()) as client:
        assert client.get("/api/v1/search").status_code == 422


def test_cors_allows_configured_origin_only():
    app = offline_app(cors_origins="https://aicampus.example.test")
    with TestClient(app) as client:
        ok = client.get("/api/v1/health/live", headers={"Origin": "https://aicampus.example.test"})
        bad = client.get("/api/v1/health/live", headers={"Origin": "https://evil.example.test"})
        preflight = client.options(
            "/api/v1/stories",
            headers={"Origin": "https://aicampus.example.test", "Access-Control-Request-Method": "POST"},
        )
    assert ok.headers["access-control-allow-origin"] == "https://aicampus.example.test"
    assert "access-control-allow-credentials" not in ok.headers
    assert "access-control-allow-origin" not in bad.headers
    assert preflight.status_code == 400  # only GET is allowed cross-origin


def test_security_headers_and_request_id():
    with TestClient(offline_app()) as client:
        res = client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-123"})
        generated = client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id\nwith newline"})
    assert res.headers["x-request-id"] == "abc-123"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert generated.headers["x-request-id"] != "bad id\nwith newline"


def test_rate_limit_is_enforced_and_health_is_exempt():
    settings = Settings(_env_file=None, app_env="test", database_url=None, rate_limit_per_minute=3)
    with TestClient(create_app(settings)) as client:
        codes = [client.get("/api/v1/does-not-exist").status_code for _ in range(4)]
        limited = client.get("/api/v1/does-not-exist")
        health = [client.get("/api/v1/health/live").status_code for _ in range(5)]
    assert codes == [404, 404, 404, 429]
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1
    assert health == [200] * 5


def test_docs_can_be_disabled():
    with TestClient(offline_app(docs_enabled=False)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
