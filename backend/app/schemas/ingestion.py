"""Admin ingestion request/response contracts (not used by the public frontend)."""

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from app.schemas.common import ApiModel


class IngestionRunIn(ApiModel):
    model_config = ConfigDict(extra="forbid")

    # Configured source keys only; omit for all sources. Arbitrary URLs cannot be submitted.
    sources: list[str] | None = Field(default=None, max_length=50)
    # true: run now and return the report (keep it to a few sources; HTTP timeouts apply).
    wait: bool = False


class IngestionRunAccepted(ApiModel):
    status: str
    sources: list[str]


class SourceStatusOut(ApiModel):
    key: str
    type: str
    name: str
    category: str
    enabled: bool
    configured: bool
    last_status: str | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_counts: dict[str, Any] = Field(default_factory=dict)
    consecutive_failures: int = 0
    pending_items: int = 0


class IngestionStatusOut(ApiModel):
    running_in_this_process: bool
    sources: list[SourceStatusOut]
