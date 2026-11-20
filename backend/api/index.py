"""Vercel entry point (optional deployment target, free Hobby plan).

Create a second Vercel project whose Root Directory is `backend`; Vercel's Python runtime serves the ASGI `app`
exported here, and `vercel.json` routes every path to it. Settings come from the project's environment variables
(see README, "Weekly editions (Phase 3.5)" -> Deployment). Long jobs (ingestion, ranking, scheduled edition
generation) do not run here: they run in GitHub Actions (.github/workflows/weekly-pipeline.yml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

__all__ = ["app"]
