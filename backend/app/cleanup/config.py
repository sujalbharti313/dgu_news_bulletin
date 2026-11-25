"""Stage 3.6 retention configuration (`config/retention.toml`), validated strictly at load time.

All retention periods of the cleanup engine are here; the Stage 3.1 periods it reports alongside (staged items,
job records) stay in Settings where Stage 3.1 reads them. `version` is a hash of the validated values and is stored
on every cleanup job and audit entry, so it is always known which policy a deletion followed."""

import hashlib
import json
import tomllib
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.intelligence.categories import BACKEND_DIR


class RetentionConfigError(Exception):
    """Invalid or missing retention configuration. `safe_message` is loggable."""

    def __init__(self, message: str):
        super().__init__(message)
        self.safe_message = message


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublishedWeek(_Strict):
    safety_delay_hours: int = Field(default=72, ge=24, le=24 * 60)
    min_story_age_days: int = Field(default=35, ge=7, le=730)
    manifest_hold_hours: int = Field(default=24, ge=1, le=24 * 30)
    delete_superseded_ranking_runs: bool = True


class Orphans(_Strict):
    enabled: bool = True
    min_age_days: int = Field(default=35, ge=7, le=730)
    manifest_hold_hours: int = Field(default=24, ge=1, le=24 * 30)


class Execution(_Strict):
    batch_size: int = Field(default=100, ge=1, le=1000)
    max_batches_per_run: int = Field(default=20, ge=1, le=500)
    max_retries: int = Field(default=5, ge=0, le=20)
    retry_backoff_minutes: int = Field(default=60, ge=1, le=24 * 60)
    stale_running_minutes: int = Field(default=60, ge=10, le=24 * 60)
    manual_runs_per_hour: int = Field(default=6, ge=1, le=60)


class RetentionConfig(_Strict):
    enabled: bool = True
    published_week: PublishedWeek = PublishedWeek()
    orphans: Orphans = Orphans()
    execution: Execution = Execution()

    @cached_property
    def version(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:16]

    def check_against(self, semantic_lookback_days: int) -> None:
        """Stage 3.4 compares new stories with the embeddings of the previous `lookback_days`: working data must
        outlive that window, or repeats of older stories would no longer be recognised."""
        minimum = semantic_lookback_days + 1
        for name, value in (
            ("published_week.min_story_age_days", self.published_week.min_story_age_days),
            ("orphans.min_age_days", self.orphans.min_age_days),
        ):
            if value < minimum:
                raise RetentionConfigError(
                    f"Retention configuration is invalid: {name} = {value} is shorter than Stage 3.4's semantic "
                    f"lookback ({semantic_lookback_days} days); use at least {minimum}."
                )


def load_retention_config(path: str | Path, *, semantic_lookback_days: int | None = None) -> RetentionConfig:
    file = Path(path)
    if not file.is_absolute():
        file = BACKEND_DIR / file
    try:
        with open(file, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise RetentionConfigError(f"Retention configuration not found: {file.name}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RetentionConfigError(f"Retention configuration is not valid TOML: {exc}") from exc
    try:
        config = RetentionConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise RetentionConfigError(f"Retention configuration is invalid: {problems}") from exc
    if semantic_lookback_days is not None:
        config.check_against(semantic_lookback_days)
    return config
