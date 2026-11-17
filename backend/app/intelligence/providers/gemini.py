"""Google Gemini provider: `models/{model}:generateContent` with JSON-schema structured output, over `httpx`.

The free tier of the Gemini API (a key from https://aistudio.google.com/apikey, no card needed) is enough for
the bulletin's volume. The key comes from GEMINI_API_KEY (server-side only); it is sent in the
`x-goog-api-key` header, never in the URL, and never logged.

Free-tier limits are per model and per minute/day. A per-minute 429 is `rate_limited` (retried after the
delay Google returns); a per-day 429 is `quota` (the run stops, or switches to AI_FALLBACK_MODEL). On the free
tier Google may use prompts to improve its products, so send only public news text through it.
"""

import copy
import re
from typing import Any

import httpx

from app.core.config import Settings
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers.base import GenerationRequest, GenerationResponse, ProviderError, Usage
from app.intelligence.providers.openai import _retry_after

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
# Finish reasons that mean the provider's own filters withheld the answer.
BLOCKED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE"}
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace local `#/$defs/...` references with the definitions themselves (the schema is not recursive).
    Gemini accepts `$ref`, but a flat schema is the most widely supported form across its models."""
    defs = schema.get("$defs", {})

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 32:
            raise ValueError("response schema is too deeply nested (recursive $ref?)")
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = walk(copy.deepcopy(defs[ref.removeprefix("#/$defs/")]), depth + 1)
                extra = {k: walk(v, depth + 1) for k, v in node.items() if k != "$ref"}
                return {**target, **extra}
            return {k: walk(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        return node

    return walk(schema)


def _error(response: httpx.Response) -> tuple[str | None, str, list[dict]]:
    """(status, message, details) from a Google API error body; never raises."""
    try:
        error = response.json().get("error") or {}
        details = error.get("details") or []
        return error.get("status"), str(error.get("message") or ""), [d for d in details if isinstance(d, dict)]
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


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        if settings.gemini_api_key is None:
            raise IntelligenceConfigError(
                "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey and put it in "
                "backend/.env (or host secrets)."
            )
        self._thinking_budget = settings.ai_thinking_budget
        self._client = httpx.AsyncClient(
            base_url=settings.ai_endpoint,
            timeout=httpx.Timeout(settings.ai_request_timeout_seconds, connect=10.0),
            headers={"x-goog-api-key": settings.gemini_api_key.get_secret_value(), "Content-Type": "application/json"},
            transport=transport,
            trust_env=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _model_path(model: str) -> str:
        model = model.removeprefix("models/")
        if not MODEL_NAME.match(model):
            raise ProviderError("bad_request", "AI_MODEL is not a valid Gemini model name.")
        return f"/models/{model}:generateContent"

    def _payload(self, request: GenerationRequest) -> dict:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        contents = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in request.messages
            if m.role != "system"
        ]
        config: dict[str, Any] = {
            "responseMimeType": "application/json",
            "responseJsonSchema": inline_refs(request.json_schema),
            "maxOutputTokens": request.max_output_tokens,
            "candidateCount": 1,
        }
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if self._thinking_budget is not None and request.model.removeprefix("models/").startswith("gemini-2.5"):
            config["thinkingConfig"] = {"thinkingBudget": self._thinking_budget}
        payload: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        path = self._model_path(request.model)
        try:
            response = await self._client.post(path, json=self._payload(request))
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "The AI provider did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", f"Could not reach the AI provider ({type(exc).__name__}).") from exc

        status = response.status_code
        if status >= 400:
            self._raise(response)

        try:
            body = response.json()
            usage_raw = body.get("usageMetadata") or {}
            usage = Usage(
                int(usage_raw.get("promptTokenCount") or 0),
                int(usage_raw.get("candidatesTokenCount") or 0) + int(usage_raw.get("thoughtsTokenCount") or 0),
            )
            candidates = body.get("candidates") or []
            model = str(body.get("modelVersion") or request.model)
            request_id = body.get("responseId")
            if not candidates:
                reason = (body.get("promptFeedback") or {}).get("blockReason") or "no answer"
                return GenerationResponse(
                    content=None, model=model, usage=usage, finish_reason="blocked",
                    refusal=f"The provider returned no answer ({reason}).", request_id=request_id,
                )  # fmt: skip
            candidate = candidates[0]
            parts = (candidate.get("content") or {}).get("parts") or []
            text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict) and not p.get("thought"))
            reason = str(candidate.get("finishReason") or "STOP")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("invalid_response", "The AI provider returned an unreadable response.") from exc

        refusal = f"The provider's safety filter withheld the answer ({reason})." if reason in BLOCKED else None
        finish = {"STOP": "stop", "MAX_TOKENS": "length"}.get(reason, reason.lower())
        return GenerationResponse(
            content=text or None,
            model=model,
            usage=usage,
            finish_reason=finish,
            refusal=refusal,
            request_id=request_id,
        )

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        status = response.status_code
        api_status, message, details = _error(response)
        if status == 429:
            quota_ids = _quota_ids(details)
            if any("PerDay" in q for q in quota_ids) or "limit: 0" in message:
                raise ProviderError(
                    "quota",
                    "The Gemini free-tier quota for this model is used up for today (it resets at midnight "
                    "Pacific time), or this model has no free quota.",
                    status_code=429,
                )
            retry_after = _retry_delay(details)
            raise ProviderError(
                "rate_limited", "The AI provider rate limit was reached.", status_code=429,
                retry_after=retry_after if retry_after is not None else _retry_after(response),
            )  # fmt: skip
        if status in (401, 403) or _reason(details) in {"API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED"}:
            raise ProviderError(
                "auth", "The AI provider rejected the API key (check GEMINI_API_KEY).", status_code=status
            )
        if status == 404:
            raise ProviderError(
                "auth", "The configured model was not found (check AI_MODEL / AI_FALLBACK_MODEL).", status_code=404
            )
        if status == 400 and api_status == "FAILED_PRECONDITION":
            raise ProviderError(
                "auth",
                "The Gemini API refused the request for this account or region (free tier not available?).",
                status_code=400,
            )
        if status >= 500 or status in (408, 409):
            raise ProviderError(
                "unavailable", f"The AI provider returned HTTP {status}.", status_code=status,
                retry_after=_retry_after(response),
            )  # fmt: skip
        raise ProviderError(
            "bad_request",
            f"The AI provider rejected the request (HTTP {status}{', ' + api_status if api_status else ''}).",
            status_code=status,
        )
