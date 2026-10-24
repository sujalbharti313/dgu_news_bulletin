import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import __version__
from app.api.deps import get_database
from app.core.errors import DatabaseNotConfiguredError
from app.db.session import Database
from app.schemas.health import HealthOut, LivenessOut

router = APIRouter(tags=["health"])
logger = logging.getLogger("app.health")


@router.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
def health(db: Annotated[Database, Depends(get_database)]):
    """Readiness: runs `SELECT 1` against the database. Returns 503 when it cannot."""
    try:
        db.ping()
        state = "ok"
    except DatabaseNotConfiguredError:
        state = "not_configured"
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"; details stay in the log
        logger.warning("health check: database unreachable (%s)", type(exc).__name__)
        state = "unavailable"
    body = HealthOut(status="ok" if state == "ok" else "degraded", version=__version__, database=state)
    return JSONResponse(body.model_dump(by_alias=True), status_code=200 if state == "ok" else 503)


@router.get("/health/live", response_model=LivenessOut)
def liveness() -> LivenessOut:
    """Liveness: the process is up. Does not touch the database."""
    return LivenessOut(status="ok", version=__version__)
