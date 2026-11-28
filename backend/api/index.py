"""Vercel entry point for the API (free Hobby plan).

A second Vercel project whose Root Directory is `backend` serves the ASGI `app` exported here; `vercel.json` routes
every path to it. Settings come from that project's environment variables (see docs/DEPLOYMENT.md). Long jobs
(ingestion, analysis, ranking, scheduled edition generation, cleanup) do not run here: they run in GitHub Actions
(.github/workflows/weekly-pipeline.yml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app as _api  # noqa: E402


def _vercel_client_ip(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The visitor's IP as Vercel's edge reports it. Vercel overwrites these headers itself (a client cannot
    spoof them), so they are trusted here, and only here: this module runs only on Vercel."""
    found = {k.lower(): v for k, v in headers}
    for name in (b"x-real-ip", b"x-vercel-forwarded-for", b"x-forwarded-for"):
        value = found.get(name)
        if value:
            ip = value.decode("latin-1").split(",")[0].strip()
            if ip and len(ip) <= 45:
                return ip
    return None


async def app(scope, receive, send):
    """Gives the rate limiter the real client IP instead of Vercel's internal proxy address, so one busy visitor
    cannot use up the limit for everyone sharing a function instance."""
    if scope.get("type") == "http":
        ip = _vercel_client_ip(scope.get("headers") or [])
        if ip:
            scope = {**scope, "client": (ip, 0)}
    await _api(scope, receive, send)


__all__ = ["app"]
