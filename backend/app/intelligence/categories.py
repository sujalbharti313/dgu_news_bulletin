"""Story categories: a small reviewed TOML file (config/intelligence_categories.toml), validated strictly."""

import hashlib
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.intelligence.errors import IntelligenceConfigError

BACKEND_DIR = Path(__file__).resolve().parents[2]
KEY_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
FrontendTopic = Literal["models", "research", "tools", "policy", "india", "careers", "campus"]


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=KEY_PATTERN, max_length=48)
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    topic: FrontendTopic


class CategorySet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_secondary: int = Field(default=2, ge=0, le=4)
    categories: list[Category] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def _unique(self) -> "CategorySet":
        keys = [c.key for c in self.categories]
        if len(set(keys)) != len(keys):
            raise ValueError("category keys must be unique")
        return self

    @cached_property
    def by_key(self) -> dict[str, Category]:
        return {c.key: c for c in self.categories}

    @property
    def keys(self) -> list[str]:
        return [c.key for c in self.categories]

    @cached_property
    def fingerprint(self) -> str:
        """Changes whenever the category set changes (part of the engine key)."""
        raw = "|".join(f"{c.key}:{c.description}" for c in self.categories) + f"|{self.max_secondary}"
        return hashlib.sha256(raw.encode()).hexdigest()[:8]


def load_categories(path: str | Path) -> CategorySet:
    file = Path(path)
    if not file.is_absolute():
        file = BACKEND_DIR / file
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IntelligenceConfigError(f"Category file not found: {file.name}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise IntelligenceConfigError(f"Category file is not valid TOML: {exc}") from exc
    try:
        return CategorySet.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise IntelligenceConfigError(f"Invalid category file ({loc}): {first['msg']}") from exc
