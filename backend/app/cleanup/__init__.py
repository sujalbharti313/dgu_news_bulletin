"""Stage 3.6: automated cleanup, data retention and published-edition preservation.

Entry points: `build_cleanup_service` (API and CLI), `python -m scripts.cleanup` (scheduler and operations) and the
admin API in app.api.v1.endpoints.admin_cleanup. See backend/README.md, "Stage 3.6".
"""

from app.cleanup.config import load_retention_config
from app.cleanup.service import CleanupService
from app.core.config import Settings
from app.db.session import Database
from app.editions.config import load_edition_config
from app.ranking.config import load_ranking_config


def build_cleanup_service(settings: Settings, db: Database, **kwargs) -> CleanupService:
    """The production wiring. The retention policy is validated against Stage 3.4's semantic lookback, so working
    data that new stories are still compared with is never removed."""
    lookback = load_ranking_config(settings.ranking_config_file).semantic.lookback_days
    config = kwargs.pop("config", None) or load_retention_config(
        settings.retention_config_file, semantic_lookback_days=lookback
    )
    edition_config = kwargs.pop("edition_config", None) or load_edition_config(settings.edition_config_file)
    return CleanupService(settings, db, config, edition_config, **kwargs)
