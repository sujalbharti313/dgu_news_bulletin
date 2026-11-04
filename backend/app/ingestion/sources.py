"""Source configuration: a small TOML file, validated strictly before anything is fetched.

Adding a source is a reviewed code change (a pull request editing the TOML file), not an API call, so
nobody can make the server fetch an arbitrary URL. Unknown keys, duplicate keys, non-HTTPS feed URLs and
internal hosts are rejected at load time.
"""

import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from app.ingestion.errors import SourceConfigError, UnsafeUrlError
from app.ingestion.url_safety import validate_url

KEY_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
BACKEND_DIR = Path(__file__).resolve().parents[2]

# What kind of publisher a source is. Used for filtering and, later, for editorial weighting.
Category = Literal["official", "research", "government", "news", "newsletter"]


class _SourceBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=KEY_PATTERN, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    category: Category
    enabled: bool = True
    # Attribution shown later. Leave unset rather than guessing: the feed's own title is used, or nothing.
    publisher: str | None = Field(default=None, min_length=1, max_length=200)
    max_items: int | None = Field(default=None, ge=1, le=1000)
    notes: str | None = Field(default=None, max_length=1000)


class RssSourceConfig(_SourceBase):
    type: Literal["rss"]
    url: str
    # Plain-HTTP feeds are refused unless explicitly allowed for that source.
    allow_http: bool = False

    @model_validator(mode="after")
    def _safe_url(self) -> "RssSourceConfig":
        try:
            validate_url(self.url, allow_http=self.allow_http)
        except UnsafeUrlError as exc:
            raise ValueError(f"url: {exc.safe_message}") from exc
        return self


class GmailSourceConfig(_SourceBase):
    type: Literal["gmail"]
    # Gmail search query (https://support.google.com/mail/answer/7190). Narrow it to the newsletter.
    query: str = Field(min_length=1, max_length=500)
    # Messages whose From address is not listed are ignored even if the query matched them.
    allowed_senders: list[str] = Field(min_length=1, max_length=20)
    parser: Literal["tldr"] = "tldr"
    max_messages: int = Field(default=10, ge=1, le=100)
    # First run only: how far back to look when no cursor exists yet.
    initial_lookback_days: int = Field(default=7, ge=1, le=60)

    @field_validator("allowed_senders")
    @classmethod
    def _lower_emails(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            value = value.strip().lower()
            if value.count("@") != 1 or " " in value or len(value) > 254:
                raise ValueError(f"not an email address: {value!r}")
            cleaned.append(value)
        return cleaned


SourceConfig = Annotated[RssSourceConfig | GmailSourceConfig, Field(discriminator="type")]


class SourcesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceConfig] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _unique(self) -> "SourcesFile":
        seen_keys: set[str] = set()
        seen_urls: set[str] = set()
        for source in self.sources:
            if source.key in seen_keys:
                raise ValueError(f"duplicate source key {source.key!r}")
            seen_keys.add(source.key)
            if isinstance(source, RssSourceConfig):
                if source.url in seen_urls:
                    raise ValueError(f"duplicate feed url for {source.key!r}")
                seen_urls.add(source.url)
        return self


_adapter = TypeAdapter(SourcesFile)


def resolve_sources_path(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else BACKEND_DIR / candidate


def parse_sources(text: str) -> list[RssSourceConfig | GmailSourceConfig]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SourceConfigError(f"Source configuration is not valid TOML: {exc}") from exc
    try:
        return _adapter.validate_python(data).sources
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors(include_input=False)
        )
        raise SourceConfigError(f"Source configuration is invalid: {problems}") from exc


def load_sources(path: str) -> list[RssSourceConfig | GmailSourceConfig]:
    resolved = resolve_sources_path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SourceConfigError(f"Source configuration file not found: {resolved.name}") from exc
    return parse_sources(text)
