"""Per-client fixed-window rate limiting, enforced in process memory.

Limits apply per worker process. With several workers or instances, the effective limit is multiplied;
use the hosting platform's edge rate limiting for a global limit. Health endpoints are exempt so
platform probes never get throttled.

The client is request.client.host. Behind a reverse proxy, run uvicorn with --proxy-headers and
--forwarded-allow-ips set to the proxy's address so that value is the real client, not the proxy.
"""

import json
import math
import threading
import time
from collections import OrderedDict

from starlette.types import ASGIApp, Receive, Scope, Send

EXEMPT_PATHS = ("/api/v1/health",)
MAX_TRACKED_CLIENTS = 10_000


class FixedWindowLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def hit(self, key: str) -> tuple[bool, int]:
        """Record one request. Returns (allowed, seconds until the window resets)."""
        now = self.clock()
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            count += 1
            self._windows[key] = (start, count)
            self._windows.move_to_end(key)
            while len(self._windows) > MAX_TRACKED_CLIENTS:
                self._windows.popitem(last=False)
            retry_after = max(1, math.ceil(self.window - (now - start)))
            return count <= self.limit, retry_after


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limit_per_minute: int):
        self.app = app
        self.limiter = FixedWindowLimiter(limit_per_minute) if limit_per_minute > 0 else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.limiter is None
            or scope["type"] != "http"
            or scope["method"] == "OPTIONS"
            or scope["path"].startswith(EXEMPT_PATHS)
        ):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        allowed, retry_after = self.limiter.hit(client[0] if client else "unknown")
        if allowed:
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {"error": {"code": "rate_limited", "message": "Too many requests. Please slow down and retry."}}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(retry_after).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
