"""FastAPI application factory. Run with: uvicorn app.main:app"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app import __version__
from app.api.v1.router import api_router
from app.core.config import Settings, describe_database_target, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import Database
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware

logger = logging.getLogger("app")


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    db = database or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # No migrations and no connection attempts here: startup never depends on the database.
        logger.info(
            "AI//CAMPUS API %s starting (env=%s, database=%s, demo content %s)",
            __version__,
            settings.app_env.value,
            describe_database_target(settings),
            "visible" if settings.expose_demo_content else "hidden",
        )
        yield
        db.dispose()

    docs = settings.docs_enabled
    app = FastAPI(
        title="AI//CAMPUS API",
        version=__version__,
        description="Public, read-only API for the AI//CAMPUS weekly AI bulletin.",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db

    register_exception_handlers(app)
    app.include_router(api_router)

    # Added innermost first; CORS is outermost so even 429 responses carry CORS headers.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RateLimitMiddleware, limit_per_minute=settings.rate_limit_per_minute)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    return app


app = create_app()
