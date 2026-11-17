"""Google Gemini provider, using Google's official Gen AI SDK (`google-genai`) against the Gemini Developer API.

- The key comes only from GEMINI_API_KEY (server-side settings). It is passed to the SDK client explicitly,
  never put in a URL, a log line, an error message or a response.
- The model is exactly AI_MODEL. There is no fallback model: a missing or retired model stops the run with a
  clear message (`model_unavailable`).
- Structured output: `response_mime_type=application/json` with `response_json_schema`. No tools, no search
  grounding, no other paid features are requested.
- The SDK's own retries are switched off; the engine owns retries, backoff, pacing and quota accounting.
- Free-tier limits are per project, per model, per minute and per day. A per-minute 429 is `rate_limited`
  (retried after the delay Google returns); a per-day 429, or a model with no free quota, is `quota` (the
  run stops and the remaining stories are marked quota_limited).
- On the free tier Google may use prompts to improve its products, so only public news text is sent.
"""

import asyncio
import contextlib
import re
from typing import Any

from app.core.config import Settings
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers.base import GenerationRequest, GenerationResponse, ProviderError, Usage

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
# Finish reasons that mean Gemini's own filters withheld the answer.
BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE"}
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")
MISSING_KEY = (
    "GEMINI_API_KEY is not set. Create a free key at https://aistudio.google.com/apikey and put it in "
    "backend/.env (or the host's secrets)."
)


def _error_parts(details: Any) -> tuple[str | None, str, list[dict]]:
    """(status, message, details list) from a Google API error body; never raises."""
    try:
        error = details.get("error", details) if isinstance(details, dict) else {}
        items = error.get("details") or []
        return error.get("status"), str(error.get("message") or ""), [d for d in items if isinstance(d, dict)]
    except Exception:  # noqa: BLE001 - an unreadable error body is not an error of its own
        return None, "", []


def _retry_delay(details: list[dict]) -> float | None:
    for detail in details:
        if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            match = _DURATION.match(str(detail.get("retryDelay") or ""))
            if match:
                return float(match.group(1))
    return None


def _quota_ids(details: list[dict]) -> list[str]:
    ids = []
    for detail in details:
        if str(detail.get("@type", "")).endswith("google.rpc.QuotaFailure"):
            ids += [str(v.get("quotaId") or "") for v in detail.get("violations") or [] if isinstance(v, dict)]
    return ids


def _reason(details: list[dict]) -> str | None:
    for detail in details:
        if str(detail.get("@type", "")).endswith("google.rpc.ErrorInfo"):
            return detail.get("reason")
    return None


def classify_api_error(code: int | None, body: Any) -> ProviderError:
    """Map a Gemini API error to a ProviderError with a safe message (no key, prompt or source text)."""
    status = code or 0
    api_status, message, details = _error_parts(body)
    if status == 429 or api_status == "RESOURCE_EXHAUSTED":
        quota_ids = _quota_ids(details)
        if any("PerDay" in q for q in quota_ids) or "limit: 0" in message:
            return ProviderError(
                "quota",
                "The Gemini free-tier quota for this model is used up for today (it resets at midnight Pacific "
                "time), or this model has no free quota for this key.",
                status_code=429,
            )
        return ProviderError(
            "rate_limited", "The Gemini rate limit was reached.", status_code=429, retry_after=_retry_delay(details)
        )
    reason = _reason(details)
    if status in (401, 403) or reason in {"API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED"}:
        return ProviderError("auth", "Gemini rejected the API key (check GEMINI_API_KEY).", status_code=status)
    if status == 404:
        return ProviderError(
            "model_unavailable",
            "The configured Gemini model was not found or is no longer available (check AI_MODEL). "
            "No other model is used automatically.",
            status_code=404,
        )
    if status == 400 and api_status == "FAILED_PRECONDITION":
        return ProviderError(
            "auth",
            "The Gemini API refused the request for this account or region (free tier not available here?).",
            status_code=400,
        )
    if status >= 500 or status in (408, 409):
        return ProviderError("unavailable", f"The Gemini API returned HTTP {status}.", status_code=status)
    suffix = f", {api_status}" if api_status else ""
    return ProviderError(
        "bad_request", f"The Gemini API rejected the request (HTTP {status}{suffix}).", status_code=status
    )


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings: Settings, *, client: Any = None):
        if settings.gemini_api_key is None:
            raise IntelligenceConfigError(MISSING_KEY)
        self._timeout = settings.ai_request_timeout_seconds
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=settings.gemini_api_key.get_secret_value(),
                http_options=types.HttpOptions(
                    timeout=int(settings.ai_request_timeout_seconds * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        self._client = client

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):  # closing is best effort
            await self._client.aio.aclose()

    @staticmethod
    def _model(model: str) -> str:
        model = model.removeprefix("models/")
        if not MODEL_NAME.match(model):
            raise ProviderError("bad_request", "AI_MODEL is not a valid Gemini model name.")
        return model

    @staticmethod
    def _config(request: GenerationRequest):
        from google.genai import types

        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        thinking = None
        if request.thinking_level:
            thinking = types.ThinkingConfig(thinking_level=types.ThinkingLevel(request.thinking_level.upper()))
        # No temperature/top_p/candidate_count: deprecated or unsupported on Gemini 3 models.
        return types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_json_schema=request.json_schema,
            max_output_tokens=request.max_output_tokens,
            thinking_config=thinking,
        )

    @staticmethod
    def _contents(request: GenerationRequest):
        from google.genai import types

        return [
            types.Content(role="model" if m.role == "assistant" else "user", parts=[types.Part(text=m.content)])
            for m in request.messages
            if m.role != "system"
        ]

    async def _guard(self, coro):
        """Run one SDK call with a hard timeout and map every failure to a ProviderError."""
        import httpx
        from google.genai import errors

        try:
            return await asyncio.wait_for(coro, timeout=self._timeout + 5)
        except TimeoutError as exc:
            raise ProviderError("timeout", "Gemini did not respond in time.") from exc
        except errors.APIError as exc:
            raise classify_api_error(exc.code, exc.details) from None
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "Gemini did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", f"Could not reach the Gemini API ({type(exc).__name__}).") from exc

    async def check_model(self, model: str) -> str:
        """Confirms the model id exists for this key (a metadata call; no generation quota used)."""
        info = await self._guard(self._client.aio.models.get(model=self._model(model)))
        return str(getattr(info, "name", None) or model)

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        model = self._model(request.model)
        response = await self._guard(
            self._client.aio.models.generate_content(
                model=model, contents=self._contents(request), config=self._config(request)
            )
        )
        try:
            meta = response.usage_metadata
            usage = Usage(
                int(getattr(meta, "prompt_token_count", 0) or 0),
                int(getattr(meta, "candidates_token_count", 0) or 0)
                + int(getattr(meta, "thoughts_token_count", 0) or 0),
            )
            version = getattr(response, "model_version", None)
            candidates = response.candidates or []
            if not candidates:
                feedback = getattr(response, "prompt_feedback", None)
                reason = getattr(getattr(feedback, "block_reason", None), "name", None) or "no answer"
                return GenerationResponse(
                    content=None,
                    model=model,
                    model_version=version,
                    usage=usage,
                    finish_reason="blocked",
                    refusal=f"Gemini returned no answer ({reason}).",
                )
            candidate = candidates[0]
            parts = (candidate.content.parts if candidate.content else None) or []
            text = "".join(p.text or "" for p in parts if not getattr(p, "thought", False))
            finish = candidate.finish_reason
            reason = str(getattr(finish, "name", finish) or "STOP")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderError("invalid_response", "Gemini returned an unreadable response.") from exc

        refusal = f"Gemini's safety filter withheld the answer ({reason})." if reason in BLOCKED else None
        finish_reason = {"STOP": "stop", "MAX_TOKENS": "length"}.get(reason, reason.lower())
        return GenerationResponse(
            content=text or None,
            model=model,
            model_version=version,
            usage=usage,
            finish_reason=finish_reason,
            refusal=refusal,
            request_id=getattr(response, "response_id", None),
        )
