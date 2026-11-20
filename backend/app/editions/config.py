"""Stage 3.5 configuration (`config/edition.toml`), validated strictly at load time."""

import hashlib
import json
import tomllib
from datetime import date, time
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.intelligence.categories import BACKEND_DIR
from app.ranking.window import edition_zone


class EditionConfigError(Exception):
    """Invalid or missing edition configuration. `safe_message` is loggable."""

    def __init__(self, message: str):
        super().__init__(message)
        self.safe_message = message


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Schedule(_Strict):
    timezone: str = "Asia/Kolkata"
    generation_weekday: int = Field(default=0, ge=0, le=6)
    generation_time: str = Field(default="06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    catch_up_weeks: int = Field(default=1, ge=0, le=4)
    start_week: date | None = None
    run_ranking_if_missing: bool = True

    @field_validator("start_week")
    @classmethod
    def _monday(cls, value: date | None) -> date | None:
        if value is not None and value.weekday() != 0:
            raise ValueError("start_week must be a Monday")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(value)
        except Exception as exc:  # noqa: BLE001 - any lookup failure means an unusable name
            if value != "Asia/Kolkata":  # edition_zone() has an exact fixed-offset fallback for India
                raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @property
    def local_time(self) -> time:
        hours, minutes = self.generation_time.split(":")
        return time(int(hours), int(minutes))

    @property
    def tz(self):
        return edition_zone(self.timezone)


class Selection(_Strict):
    target_stories: int = Field(default=10, ge=1, le=20)
    min_stories: int = Field(default=3, ge=0, le=20)


class Generation(_Strict):
    max_attempts: int = Field(default=4, ge=1, le=20)
    retry_backoff_minutes: int = Field(default=60, ge=1, le=1440)
    use_ai: bool = True
    fallback_to_stage33_text: bool = True
    max_output_tokens: int = Field(default=8192, ge=1024, le=32_000)


class Limits(_Strict):
    title: int = Field(default=120, ge=20, le=200)
    introduction: int = Field(default=1200, ge=100, le=4000)
    editorial_note: int = Field(default=600, ge=0, le=2000)
    headline: int = Field(default=200, ge=20, le=300)
    summary: int = Field(default=600, ge=50, le=2000)
    technical_explanation: int = Field(default=1800, ge=100, le=6000)
    why_it_matters: int = Field(default=900, ge=50, le=3000)
    student_relevance: int = Field(default=900, ge=50, le=3000)
    project_idea: int = Field(default=500, ge=50, le=2000)
    review_note: int = Field(default=1000, ge=50, le=4000)


class EditionConfig(_Strict):
    schedule: Schedule = Schedule()
    selection: Selection = Selection()
    generation: Generation = Generation()
    limits: Limits = Limits()

    @cached_property
    def version(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:16]


def load_edition_config(path: str | Path) -> EditionConfig:
    file = Path(path)
    if not file.is_absolute():
        file = BACKEND_DIR / file
    try:
        with open(file, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise EditionConfigError(f"Edition configuration not found: {file.name}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise EditionConfigError(f"Edition configuration is not valid TOML: {exc}") from exc
    try:
        return EditionConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise EditionConfigError(f"Edition configuration is invalid: {problems}") from exc
