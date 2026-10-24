"""The API must satisfy the frontend's TypeScript types (src/lib/types.ts), read straight from the repo."""

import re
from pathlib import Path

import pytest

from app.seed.runner import run_seed

TYPES_TS = Path(__file__).resolve().parents[2] / "src" / "lib" / "types.ts"


def interfaces() -> dict[str, tuple[set[str], set[str]]]:
    """interface name -> (required fields, optional fields)."""
    source = TYPES_TS.read_text(encoding="utf-8")
    found = {}
    for name, body in re.findall(r"export interface (\w+) \{(.*?)\n\}", source, re.S):
        required, optional = set(), set()
        for field, opt in re.findall(r"^\s+(\w+)(\?)?:", body, re.M):
            (optional if opt else required).add(field)
        found[name] = (required, optional)
    return found


def assert_satisfies(payload: dict, interface: str):
    required, optional = interfaces()[interface]
    missing = required - payload.keys()
    assert not missing, f"{interface} is missing {missing}"
    return required | optional


pytestmark = pytest.mark.skipif(not TYPES_TS.exists(), reason="frontend sources not present")


def test_every_endpoint_satisfies_the_frontend_types(demo_client, db_session):
    run_seed(db_session, reference=True, demo=True)
    get = lambda path: demo_client.get(f"/api/v1{path}").json()  # noqa: E731

    story = get("/stories/small-open-models-on-a-laptop")
    assert_satisfies(story, "Story")
    assert_satisfies(story["impact"], "StudentImpact")
    for item in get("/stories?pageSize=100")["items"]:
        assert_satisfies(item, "Story")
    for item in get("/search?q=agents")["items"]:
        assert_satisfies(item, "Story")
    for item in get("/editions")["items"]:
        assert_satisfies(item, "Edition")
    current = get("/editions/current")
    assert_satisfies(current, "Edition")
    assert_satisfies(get("/briefings/latest"), "Briefing")
    for path, name in [
        ("/tools?pageSize=100", "Tool"),
        ("/research", "ResearchPaper"),
        ("/india", "IndiaEntry"),
        ("/career-signals", "CareerSignal"),
        ("/campus", "CampusItem"),
    ]:
        items = get(path)["items"]
        assert items, path
        for item in items:
            assert_satisfies(item, name)


def test_seeded_api_content_matches_the_frontend_sample_files(demo_client, db_session):
    """Switching NEXT_PUBLIC_API_BASE_URL on with seeded data must show the same content as the sample adapter."""
    run_seed(db_session, reference=True, demo=True)
    import json

    from app.seed.runner import DATA_DIR

    demo = json.loads((DATA_DIR / "demo.json").read_text())
    expected = {s["slug"]: s for s in demo["stories"]}
    for item in demo_client.get("/api/v1/stories?pageSize=100").json()["items"]:
        src = expected[item["slug"]]
        for key in ("headline", "dek", "body", "topic", "editionId", "publishedAt", "readingMinutes", "impact"):
            assert item[key] == src[key], (item["slug"], key)
        assert item["featured"] == bool(src.get("featured"))
        assert item["sourceName"] == src["sourceName"] and item["sourceUrl"] is None
