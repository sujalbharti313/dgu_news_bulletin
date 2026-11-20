"""CORS by path: the public API stays GET-only cross-origin; only the Stage 3.5 editorial admin API (which needs a
Supabase admin token in the Authorization header) also accepts POST/PUT and the Authorization header from the
configured frontend origins. No credentials (cookies) are ever allowed cross-origin."""

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

EDITORIAL_ADMIN_PREFIX = "/api/v1/admin/editions"


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
        if scope["type"] == "http" and scope["path"].startswith(EDITORIAL_ADMIN_PREFIX):
            await self.admin(scope, receive, send)
        else:
            await self.public(scope, receive, send)
