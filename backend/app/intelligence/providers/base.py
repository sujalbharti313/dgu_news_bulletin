"""The provider interface. A provider turns one structured-output request into raw JSON text plus usage.

The only real provider is Google Gemini (`gemini.py`); tests use a scripted fake with the same interface.
Providers do not retry and do not validate content: retries, backoff, rate-limit pauses, quota accounting
and validation belong to the engine.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ErrorKind = Literal[
    "timeout", "rate_limited", "unavailable", "auth", "quota", "model_unavailable", "bad_request", "invalid_response"
]
# Errors that stop the whole run: retrying other stories would fail the same way. `quota` additionally marks
# the remaining stories quota_limited; `model_unavailable` never switches to another model.
FATAL_KINDS = {"auth", "quota", "model_unavailable"}
RETRYABLE_KINDS = {"timeout", "rate_limited", "unavailable"}


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class GenerationRequest:
    model: str
    messages: list[Message]
    schema_name: str
    json_schema: dict[str, Any]
    max_output_tokens: int
    thinking_level: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass(frozen=True)
class GenerationResponse:
    content: str | None
    model: str
    model_version: str | None = None
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    refusal: str | None = None
    request_id: str | None = None


class ProviderError(Exception):
    """A provider failure. `safe_message` is loggable: it never contains keys, prompts or source text."""

    def __init__(
        self,
        kind: ErrorKind,
        safe_message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        usage: Usage | None = None,
    ):
        super().__init__(safe_message)
        self.kind = kind
        self.safe_message = safe_message
        self.status_code = status_code
        self.retry_after = retry_after
        self.usage = usage or Usage()
        self.calls = 0  # set by the engine: requests made before the error was final

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL_KINDS


class IntelligenceProvider(Protocol):
    name: str

    async def generate(self, request: GenerationRequest) -> GenerationResponse: ...

    async def check_model(self, model: str) -> str: ...

    async def aclose(self) -> None: ...
