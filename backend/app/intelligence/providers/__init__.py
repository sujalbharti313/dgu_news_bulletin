"""The Gemini provider behind a small interface (`base.IntelligenceProvider`), so tests can use a fake."""

from app.core.config import Settings
from app.intelligence.providers.base import IntelligenceProvider


def build_provider(settings: Settings) -> IntelligenceProvider:
    from app.intelligence.providers.gemini import GeminiProvider

    return GeminiProvider(settings)
