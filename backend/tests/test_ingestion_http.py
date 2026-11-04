"""SafeHttpClient: retries with backoff, timeouts, size limits, redirects re-validated, no secret leakage."""

import httpx
import pytest

from app.ingestion.errors import FetchError, ResponseTooLargeError, RetryableFetchError, UnsafeUrlError
from tests.ingestion_helpers import make_http

FEED = "https://feeds.example.com/rss"


def test_retries_5xx_with_exponential_backoff_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, content=b"ok")

    sleeps: list[float] = []
    http = make_http(handler, sleeps=sleeps, max_retries=3, backoff_base_seconds=1.0)
    assert http.get(FEED).content == b"ok"
    assert len(calls) == 3 and http.attempts == 3 and http.retries == 2
    assert sleeps == [1.0, 2.0]  # base * 2**attempt (jitter fixed at its maximum)


def test_gives_up_after_max_retries():
    sleeps: list[float] = []
    http = make_http(lambda r: httpx.Response(500), sleeps=sleeps, max_retries=2)
    with pytest.raises(RetryableFetchError):
        http.get(FEED)
    assert http.attempts == 3 and len(sleeps) == 2


def test_retry_after_is_honoured_but_capped():
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    sleeps: list[float] = []
    http = make_http(lambda r: next(responses), sleeps=sleeps, backoff_cap_seconds=5.0)
    http.get(FEED)
    assert sleeps == [5.0]


def test_timeouts_are_retried():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=b"x")

    http = make_http(handler)
    assert http.get(FEED).content == b"x"


def test_404_is_not_retried():
    http = make_http(lambda r: httpx.Response(404))
    with pytest.raises(FetchError) as info:
        http.get(FEED)
    assert info.value.status_code == 404 and http.attempts == 1


def test_304_is_returned_for_conditional_requests():
    assert make_http(lambda r: httpx.Response(304)).get(FEED).status_code == 304


def test_response_size_limit_streaming_and_declared():
    big = make_http(lambda r: httpx.Response(200, content=b"x" * 20_000), max_response_bytes=10_000)
    with pytest.raises(ResponseTooLargeError):
        big.get(FEED)
    declared = make_http(
        lambda r: httpx.Response(200, headers={"Content-Length": "999999999"}, content=b""), max_response_bytes=10_000
    )
    with pytest.raises(ResponseTooLargeError):
        declared.get(FEED)


def test_redirect_to_internal_address_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest/meta-data/"})

    with pytest.raises(UnsafeUrlError):
        make_http(handler).get(FEED)


def test_redirect_to_host_resolving_privately_is_blocked():
    def resolver(host, port):
        return ["10.0.0.7"] if host == "internal.example.com" else ["93.184.215.14"]

    def handler(request):
        return httpx.Response(301, headers={"Location": "https://internal.example.com/admin"})

    with pytest.raises(UnsafeUrlError):
        make_http(handler, resolver=resolver).get(FEED)


def test_public_redirect_is_followed_and_redirect_loops_stop():
    def handler(request):
        if request.url.path == "/rss":
            return httpx.Response(301, headers={"Location": "/feed.xml"})
        return httpx.Response(200, content=b"feed")

    response = make_http(handler).get(FEED)
    assert response.content == b"feed" and response.url.endswith("/feed.xml")

    loop = make_http(lambda r: httpx.Response(302, headers={"Location": FEED}), max_redirects=2)
    with pytest.raises(FetchError, match="Too many redirects"):
        loop.get(FEED)


def test_unsafe_initial_url_never_reaches_transport():
    calls = []
    http = make_http(lambda r: calls.append(r) or httpx.Response(200))
    for url in ("http://feeds.example.com/rss", "https://localhost/", "file:///etc/passwd"):
        with pytest.raises(UnsafeUrlError):
            http.get(url)
    assert calls == []


def test_no_cookies_or_proxy_env_and_user_agent_set(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, headers={"Set-Cookie": "a=b"})

    http = make_http(handler, user_agent="AI-CAMPUS-Test")
    http.get(FEED)
    http.get(FEED)
    assert seen["user-agent"] == "AI-CAMPUS-Test"
    assert "cookie" not in {k.lower() for k in seen}


def test_retry_logs_never_include_query_or_auth(caplog):
    import logging

    # Alembic's fileConfig (run by the migration tests) disables loggers that existed before it.
    logging.getLogger("app.ingestion.http").disabled = False
    responses = iter([httpx.Response(503), httpx.Response(200)])
    http = make_http(lambda r: next(responses))
    with caplog.at_level("WARNING"):
        http.get(FEED + "?token=SECRET123", headers={"Authorization": "Bearer SECRET456"})
    text = caplog.text
    assert "retrying" in text and "SECRET123" not in text and "SECRET456" not in text
