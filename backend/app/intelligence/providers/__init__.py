"""AI providers behind one small interface (`base.IntelligenceProvider`). Choose with AI_PROVIDER."""

from app.core.config import Settings
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.providers.base import IntelligenceProvider


def build_provider(settings: Settings) -> IntelligenceProvider:
    if settings.ai_provider == "openai":
        from app.intelligence.providers.openai import OpenAIProvider

        return OpenAIProvider(settings)
    raise IntelligenceConfigError(f"Unknown AI_PROVIDER {settings.ai_provider!r}. Supported: openai.")
