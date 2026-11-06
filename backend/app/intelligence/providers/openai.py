"""OpenAI provider: Chat Completions with strict JSON-schema structured output, over plain `httpx`.

No SDK dependency, so any OpenAI-compatible endpoint works by changing AI_BASE_URL. The API key comes
from OPENAI_API_KEY (server-side only); it is sent in the Authorization header and never logged.
An OpenAI API key is billed per token on platform.openai.com, separately from any ChatGPT subscription.
"""

import email.utils
import time

import httpx

from app.core.config import Settings
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers.base import GenerationRequest, GenerationResponse, ProviderError, Usage


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after-ms")
    if raw:
        try:
            return max(0.0, float(raw) / 1000)
        except ValueError:
            pass
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(raw) if raw else None
        return max(0.0, parsed.timestamp() - time.time()) if parsed else None


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error") or {}
        return error.get("code") or error.get("type")
    except Exception:  # noqa: BLE001 - an unreadable error body is not an error of its own
        return None


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        if settings.openai_api_key is None:
            raise IntelligenceConfigError("OPENAI_API_KEY is not set (backend/.env or host secrets).")
        self._client = httpx.AsyncClient(
            base_url=settings.ai_base_url,
            timeout=httpx.Timeout(settings.ai_request_timeout_seconds, connect=10.0),
            headers={
                "Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            transport=transport,
            trust_env=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(self, request: GenerationRequest) -> dict:
        payload = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name, "strict": True, "schema": request.json_schema},
            },
            "max_completion_tokens": request.max_output_tokens,
            "store": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        try:
            response = await self._client.post("/chat/completions", json=self._payload(request))
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "The AI provider did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", f"Could not reach the AI provider ({type(exc).__name__}).") from exc

        status = response.status_code
        if status == 429:
            code = _error_code(response)
            if code == "insufficient_quota":
                raise ProviderError(
                    "quota", "The AI provider account has no remaining quota or credit.", status_code=429
                )
            raise ProviderError(
                "rate_limited", "The AI provider rate limit was reached.", status_code=429,
                retry_after=_retry_after(response),
            )  # fmt: skip
        if status in (401, 403):
            raise ProviderError("auth", "The AI provider rejected the API key or its permissions.", status_code=status)
        if status == 404:
            raise ProviderError(
                "auth", "The configured model or endpoint was not found (check AI_MODEL).", status_code=404
            )
        if status >= 500 or status in (408, 409):
            raise ProviderError(
                "unavailable", f"The AI provider returned HTTP {status}.", status_code=status,
                retry_after=_retry_after(response),
            )  # fmt: skip
        if status >= 400:
            code = _error_code(response)
            raise ProviderError(
                "bad_request", f"The AI provider rejected the request (HTTP {status}{', ' + code if code else ''}).",
                status_code=status,
            )  # fmt: skip

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice.get("message") or {}
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("invalid_response", "The AI provider returned an unreadable response.") from exc
        return GenerationResponse(
            content=message.get("content"),
            model=str(body.get("model") or request.model),
            usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
            finish_reason=choice.get("finish_reason"),
            refusal=message.get("refusal"),
            request_id=response.headers.get("x-request-id"),
        )
