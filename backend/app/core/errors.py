"""Application errors and the handlers that turn every failure into the same JSON shape.

    {"error": {"code": "not_found", "message": "...", "details": [...]}}

Internal exception text, SQL and stack traces are logged server-side only.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")

_HTTP_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    413: "payload_too_large",
    429: "rate_limited",
}


class ApiError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class DatabaseUnavailableError(ApiError):
    status_code = 503
    code = "database_unavailable"

    def __init__(self, message: str = "The database is temporarily unavailable. Please try again shortly."):
        super().__init__(message)


class DatabaseNotConfiguredError(DatabaseUnavailableError):
    def __init__(self) -> None:
        super().__init__("The database is not configured for this deployment.")


def error_body(code: str, message: str, details: list | None = None) -> dict:
    body: dict = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}


def _request_id(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        if isinstance(exc, DatabaseUnavailableError):
            logger.warning("database unavailable", extra={"request_id": _request_id(request)})
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only location, message and type: the rejected input itself is not echoed back.
        details = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body("validation_error", "One or more request parameters are invalid.", details), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        if exc.status_code == 404 and message == "Not Found":
            message = "The requested resource does not exist."
        return JSONResponse(error_body(code, message), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    @app.exception_handler(PoolTimeoutError)
    async def _db_unavailable(request: Request, exc: Exception) -> JSONResponse:
        logger.error("database error: %s", type(exc).__name__, extra={"request_id": _request_id(request)})
        err = DatabaseUnavailableError()
        return JSONResponse(error_body(err.code, err.message), status_code=err.status_code)

    @app.exception_handler(DBAPIError)
    async def _db_error(request: Request, exc: DBAPIError) -> JSONResponse:
        if exc.connection_invalidated:
            return await _db_unavailable(request, exc)
        logger.exception("database error", extra={"request_id": _request_id(request)})
        return JSONResponse(error_body("internal_error", "An unexpected error occurred."), status_code=500)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"request_id": _request_id(request)})
        return JSONResponse(error_body("internal_error", "An unexpected error occurred."), status_code=500)
