"""Importing this package registers every table on Base.metadata (Alembic relies on it)."""

from app.models.briefing import Briefing
from app.models.edition import Edition
from app.models.editorial import EditionAuditEntry, EditionItem, EditionRevision, EditorialAdmin
from app.models.ingestion import IngestionItem, IngestionJob
from app.models.intelligence import AiUsageDaily, StoryAnalysisRecord
from app.models.ranking import RankingRun, StoryEmbedding, StoryVerificationRecord
from app.models.reference import CampusItem, CareerSignal, IndiaEntry
from app.models.research import ResearchPaper
from app.models.source import Source
from app.models.story import Story
from app.models.tool import Tool

__all__ = [
    "AiUsageDaily",
    "Briefing",
    "CampusItem",
    "CareerSignal",
    "Edition",
    "EditionAuditEntry",
    "EditionItem",
    "EditionRevision",
    "EditorialAdmin",
    "IndiaEntry",
    "IngestionItem",
    "IngestionJob",
    "RankingRun",
    "ResearchPaper",
    "Source",
    "Story",
    "StoryAnalysisRecord",
    "StoryEmbedding",
    "StoryVerificationRecord",
    "Tool",
]
