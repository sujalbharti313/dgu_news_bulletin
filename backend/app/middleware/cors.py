"""CORS by path: the public API stays GET-only cross-origin; only the editorial admin APIs that the /admin pages call
with a Supabase admin token in the Authorization header (Stage 3.5 editions and Stage 3.6 cleanup) also accept
POST/PUT and the Authorization header from the configured frontend origins. No credentials (cookies) are ever
allowed cross-origin. The other admin endpoints (ingestion, processing, analysis, ranking) are server-to-server
only and get no cross-origin Authorization header."""

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

EDITORIAL_ADMIN_PREFIXES = ("/api/v1/admin/editions", "/api/v1/admin/cleanup")


class PathAwareCORS:
    def __init__(self, app: ASGIApp, *, origins: list[str]):
        common = {
            "allow_origins": origins,
            "allow_credentials": False,
            "expose_headers": ["X-Request-ID", "Retry-After"],
            "max_age": 600,
        }
        self.public = CORSMiddleware(
            app, allow_methods=["GET"], allow_headers=["Accept", "Content-Type", "X-Request-ID"], **common
        )
        self.admin = CORSMiddleware(
            app,
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["Accept", "Content-Type", "X-Request-ID", "Authorization"],
            **common,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _is_editorial_admin(scope["path"]):
            await self.admin(scope, receive, send)
        else:
            await self.public(scope, receive, send)


def _is_editorial_admin(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in EDITORIAL_ADMIN_PREFIXES)
