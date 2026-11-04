"""The only way ingestion code talks to the network.

Every request: SSRF-validated URL and DNS answers (re-checked on each redirect hop), post-connect peer
address check, connect/read timeouts plus an overall deadline, a response size cap, no cookies, no
environment proxies by default, and bounded retries with exponential backoff and jitter on timeouts,
connection errors, 429 and 5xx. Request headers (which may carry an OAuth access token) are never logged.
"""

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import Settings
from app.ingestion.errors import (
    FetchError,
    IngestionError,
    ResponseTooLargeError,
    RetryableFetchError,
    UnsafeUrlError,
)
from app.ingestion.url_safety import (
    Resolver,
    check_peer_address,
    resolve_public_addresses,
    system_resolver,
    validate_url,
)

logger = logging.getLogger("app.ingestion.http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class FetchResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str

    def json(self):
        import json

        return json.loads(self.content)


def _host_label(url: str) -> str:
    """Scheme and host only, for logs: never paths or query strings (they can carry tokens)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}"


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class SafeHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 5_000_000,
        max_retries: int = 3,
        user_agent: str = "AI-CAMPUS-Ingestion/0.3",
        trust_env: bool = False,
        max_redirects: int = 3,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.max_redirects = max_redirects
        self.backoff_base = backoff_base_seconds
        self.backoff_cap = backoff_cap_seconds
        self.resolver = resolver
        self.sleep = sleep
        self.jitter = jitter
        self.attempts = 0  # network attempts made through this client, for job bookkeeping
        self.retries = 0
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
            follow_redirects=False,
            trust_env=trust_env,
            headers={"User-Agent": user_agent},
            transport=transport,
            # Never keep cookies between requests or sources.
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )

    @classmethod
    def from_settings(cls, settings: Settings, **overrides) -> "SafeHttpClient":
        values = {
            "timeout_seconds": settings.ingestion_http_timeout_seconds,
            "max_response_bytes": settings.ingestion_max_response_bytes,
            "max_retries": settings.ingestion_max_retries,
            "user_agent": settings.ingestion_user_agent,
            "trust_env": settings.ingestion_http_trust_env,
        }
        values.update(overrides)
        return cls(**values)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SafeHttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- public API -------------------------------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping | None = None,
        allow_http: bool = False,
        ok_statuses: frozenset[int] = frozenset({304}),
    ) -> FetchResponse:
        return self._with_retries(
            "GET", url, headers=headers, params=params, data=None, allow_http=allow_http, ok_statuses=ok_statuses
        )

    def post_form(
        self, url: str, data: Mapping[str, str], *, headers: Mapping[str, str] | None = None
    ) -> FetchResponse:
        return self._with_retries(
            "POST", url, headers=headers, params=None, data=data, allow_http=False, ok_statuses=frozenset()
        )

    # -- internals --------------------------------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        delay = min(self.backoff_cap, self.backoff_base * (2**attempt)) * (0.5 + self.jitter() / 2)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.backoff_cap))
        return delay

    def _with_retries(self, method, url, *, headers, params, data, allow_http, ok_statuses) -> FetchResponse:
        attempt = 0
        while True:
            try:
                return self._follow(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=data,
                    allow_http=allow_http,
                    ok_statuses=ok_statuses,
                )
            except RetryableFetchError as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self._backoff(attempt, getattr(exc, "retry_after", None))
                logger.warning(
                    "retrying %s %s after %s (attempt %d of %d, waiting %.1fs)",
                    method,
                    _host_label(url),
                    exc.safe_message,
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                )
                self.retries += 1
                attempt += 1
                self.sleep(delay)

    def _follow(self, method, url, *, headers, params, data, allow_http, ok_statuses) -> FetchResponse:
        current = url
        for _hop in range(self.max_redirects + 1):
            response = self._send_once(
                method, current, headers=headers, params=params, data=data, allow_http=allow_http
            )
            if response.status_code in REDIRECT_STATUS and method == "GET":
                location = response.headers.get("location")
                if not location:
                    raise FetchError("Redirect without a Location header.", response.status_code)
                current = urljoin(current, location)
                params = None  # already part of the URL we were redirected from
                continue
            return self._check_status(response, ok_statuses)
        raise FetchError("Too many redirects.")

    def _check_status(self, response: FetchResponse, ok_statuses: frozenset[int]) -> FetchResponse:
        status = response.status_code
        if 200 <= status < 300 or status in ok_statuses:
            return response
        if status in RETRYABLE_STATUS:
            err = RetryableFetchError(f"HTTP {status}", status)
            err.retry_after = _retry_after_seconds(response.headers.get("retry-after"))
            raise err
        raise FetchError(f"HTTP {status}", status)

    def _send_once(self, method, url, *, headers, params, data, allow_http) -> FetchResponse:
        validate_url(url, allow_http=allow_http)
        parts = urlsplit(url)
        resolve_public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), self.resolver)
        self.attempts += 1
        deadline = time.monotonic() + self.timeout_seconds * 2
        try:
            with self._client.stream(method, url, headers=headers, params=params, data=data) as response:
                stream = response.extensions.get("network_stream")
                if stream is not None and hasattr(stream, "get_extra_info"):
                    check_peer_address(stream.get_extra_info("server_addr"))
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
                    raise ResponseTooLargeError("Response exceeds the size limit.")
                body = bytearray()
                if response.status_code not in REDIRECT_STATUS:
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_response_bytes:
                            raise ResponseTooLargeError("Response exceeds the size limit.")
                        if time.monotonic() > deadline:
                            raise RetryableFetchError("Response took too long.")
                return FetchResponse(response.status_code, response.headers, bytes(body), str(response.url))
        except IngestionError:
            raise
        except httpx.TimeoutException as exc:
            raise RetryableFetchError(f"Timed out ({type(exc).__name__}).") from exc
        except httpx.TransportError as exc:
            raise RetryableFetchError(f"Connection failed ({type(exc).__name__}).") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP error ({type(exc).__name__}).") from exc


__all__ = ["FetchResponse", "SafeHttpClient", "UnsafeUrlError"]
