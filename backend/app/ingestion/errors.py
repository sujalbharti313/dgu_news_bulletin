"""Ingestion failures. Messages are safe to log and store: no URLs with credentials, no tokens, no content."""


class IngestionError(Exception):
    """Base class. `safe_message` is what reaches logs and ingestion_jobs.error_summary."""

    retryable = False

    def __init__(self, safe_message: str):
        super().__init__(safe_message)
        self.safe_message = safe_message


class UnsafeUrlError(IngestionError):
    """The URL is not allowed (scheme, credentials, port, or a private/internal destination)."""


class FetchError(IngestionError):
    """The remote server could not be reached or answered with an error."""

    def __init__(self, safe_message: str, status_code: int | None = None):
        super().__init__(safe_message)
        self.status_code = status_code


class RetryableFetchError(FetchError):
    retryable = True


class ResponseTooLargeError(FetchError):
    pass


class FeedParseError(IngestionError):
    pass


class SourceConfigError(IngestionError):
    pass


class GmailAuthError(IngestionError):
    """OAuth refresh failed (revoked or expired refresh token, wrong client). Needs a human."""
