from app.core.errors import ApiError


class EditionError(ApiError):
    """A refused editorial action. The message is safe to show to an admin (no SQL, secrets or stack traces)."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "edition_error", details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


def not_found() -> EditionError:
    return EditionError("Edition not found.", status_code=404, code="not_found")


def stale(current: int) -> EditionError:
    return EditionError(
        f"This edition changed since you loaded it (it is now at revision {current}). Reload and try again.",
        status_code=409,
        code="stale_revision",
    )


def invalid_state(message: str) -> EditionError:
    return EditionError(message, status_code=409, code="invalid_state")


def validation_failed(issues: list) -> EditionError:
    return EditionError(
        "The edition has validation errors; fix them first.",
        status_code=422,
        code="edition_invalid",
        details=[{"loc": [i["path"]] if i.get("path") else [], "msg": i["message"], "type": i["code"]} for i in issues],
    )
