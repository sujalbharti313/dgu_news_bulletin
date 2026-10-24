"""Importing this package registers every table on Base.metadata (Alembic relies on it)."""

from app.models.briefing import Briefing
from app.models.edition import Edition
from app.models.ingestion import IngestionJob
from app.models.reference import CampusItem, CareerSignal, IndiaEntry
from app.models.research import ResearchPaper
from app.models.source import Source
from app.models.story import Story
from app.models.tool import Tool

__all__ = [
    "Briefing",
    "CampusItem",
    "CareerSignal",
    "Edition",
    "IndiaEntry",
    "IngestionJob",
    "ResearchPaper",
    "Source",
    "Story",
    "Tool",
]
