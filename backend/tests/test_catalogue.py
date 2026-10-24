from datetime import date

from app.models import CampusItem, CareerSignal, IndiaEntry
from tests import factories as f


def test_tools_filter_search_and_hide_drafts(client, db_session):
    f.tool(db_session, slug="alpha-coder", name="Alpha Coder", category="coding", display_order=1)
    f.tool(db_session, slug="beta-media", name="Beta Media", category="media", display_order=0)
    f.tool(db_session, slug="hidden-tool", status="draft")

    all_tools = client.get("/api/v1/tools").json()
    assert [t["id"] for t in all_tools["items"]] == ["beta-media", "alpha-coder"]
    assert [t["id"] for t in client.get("/api/v1/tools", params={"category": "coding"}).json()["items"]] == [
        "alpha-coder"
    ]
    assert [t["id"] for t in client.get("/api/v1/tools", params={"q": "beta"}).json()["items"]] == ["beta-media"]
    # LIKE wildcards are literal
    assert client.get("/api/v1/tools", params={"q": "%"}).json()["total"] == 0
    assert client.get("/api/v1/tools/hidden-tool").status_code == 404
    tool = client.get("/api/v1/tools/alpha-coder").json()
    assert tool["maker"] == "Test Provider" and tool["pricingNotes"] is None and tool["lastVerifiedAt"] is None
    assert client.get("/api/v1/tools", params={"category": "spaceships"}).status_code == 422


def test_research_sorting_lookup_and_interpretation_fields(client, db_session):
    old = f.paper(db_session, slug="old-paper", published_on=date(2017, 6, 12), topic="research")
    new = f.paper(db_session, slug="new-paper", published_on=date(2022, 1, 28))
    f.paper(db_session, slug="draft-paper", status="draft")

    assert [p["id"] for p in client.get("/api/v1/research").json()["items"]] == ["new-paper", "old-paper"]
    assert [p["id"] for p in client.get("/api/v1/research", params={"sort": "oldest"}).json()["items"]] == [
        "old-paper",
        "new-paper",
    ]
    assert client.get(f"/api/v1/research/{old.id}").json()["id"] == "old-paper"
    body = client.get(f"/api/v1/research/{new.slug}").json()
    assert body["demonstrated"] == "Findings (test)."  # the paper's own result
    assert body["plainLanguage"] == "Plain summary (test)."  # our interpretation
    assert body["summaryOrigin"] == "editorial"
    assert client.get("/api/v1/research/draft-paper").status_code == 404
    assert client.get("/api/v1/research", params={"topic": "research"}).json()["total"] == 1


def test_reference_collections(client, demo_client, db_session):
    db_session.add_all(
        [
            IndiaEntry(
                slug="lab-one",
                name="Lab One",
                kind="research_lab",
                focus="F",
                description="D",
                url="https://lab.example.test",
                status="published",
            ),
            IndiaEntry(
                slug="draft-entry",
                name="Draft",
                kind="startup",
                focus="F",
                description="D",
                url="https://draft.example.test",
            ),
            CareerSignal(
                slug="sig-one",
                kind="skill",
                title="T",
                description="D",
                start_with="S",
                related_topics=["india", "tools"],
                status="published",
            ),
            CareerSignal(
                slug="sig-two",
                kind="project",
                title="T2",
                description="D",
                start_with="S",
                related_topics=["models"],
                status="published",
            ),
            CampusItem(slug="real-club", kind="club", title="Club", description="D", status="published"),
            CampusItem(
                slug="demo-format", kind="workshop", title="Format", description="D", status="published", is_demo=True
            ),
        ]
    )
    db_session.flush()

    india = client.get("/api/v1/india").json()["items"]
    assert india == [
        {
            "id": "lab-one",
            "name": "Lab One",
            "kind": "Research lab",
            "focus": "F",
            "description": "D",
            "url": "https://lab.example.test",
        }
    ]
    assert [s["id"] for s in client.get("/api/v1/career-signals", params={"topic": "india"}).json()["items"]] == [
        "sig-one"
    ]
    assert [s["id"] for s in client.get("/api/v1/career-signals", params={"kind": "project"}).json()["items"]] == [
        "sig-two"
    ]

    campus = client.get("/api/v1/campus").json()["items"]
    assert [(c["id"], c["kind"], c["provenance"]) for c in campus] == [("real-club", "Club", "live")]
    assert {c["id"] for c in demo_client.get("/api/v1/campus").json()["items"]} == {"real-club", "demo-format"}
