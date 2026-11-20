"""Stage 3.5: weekly edition generation, admin editorial review, approval and controlled publishing.

Entry points: `build_service` (API and CLI), `python -m scripts.edition` (scheduler and operations), and the
admin API in app.api.v1.endpoints.admin_editions. See backend/README.md, "Stage 3.5".
"""

from app.core.config import Settings
from app.db.session import Database
from app.editions.config import load_edition_config
from app.editions.service import EditionService


def build_service(settings: Settings, db: Database, **kwargs) -> EditionService:
    """The production wiring: Gemini composer on the shared free-tier ledger, and Stage 3.4 on demand."""
    config = kwargs.pop("config", None) or load_edition_config(settings.edition_config_file)

    def composer_factory():
        if settings.gemini_api_key is None:
            return None
        from app.editions.compose import EditionComposer
        from app.intelligence.providers.gemini import GeminiProvider
        from app.intelligence.quota import SqlUsageLedger

        return EditionComposer(settings, config, GeminiProvider(settings), ledger=SqlUsageLedger(db))

    def rank_week(week_start):
        from app.ranking.service import run_ranking

        run_ranking(settings, db, week=week_start.isoformat())

    kwargs.setdefault("composer_factory", composer_factory)
    kwargs.setdefault("rank_week", rank_week)
    return EditionService(settings, db, config, **kwargs)
