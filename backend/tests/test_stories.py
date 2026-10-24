from datetime import date, timedelta

from tests import factories as f


def slugs(res) -> list[str]:
    return [s["slug"] for s in res.json()["items"]]


def test_lists_published_stories_with_pagination(client, db_session):
    src, ed = f.source(db_session), f.edition(db_session)
    made = [f.story(db_session, edition=ed, source=src, published_at=f.now() - timedelta(hours=i)) for i in range(1, 6)]

    first = client.get("/api/v1/stories", params={"pageSize": 2})
    second = client.get("/api/v1/stories", params={"pageSize": 2, "page": 2})
    beyond = client.get("/api/v1/stories", params={"pageSize": 2, "page": 9})

    assert first.status_code == 200
    body = first.json()
    assert (body["page"], body["pageSize"], body["total"], body["totalPages"]) == (1, 2, 5, 3)
    assert slugs(first) == [made[0].slug, made[1].slug]  # newest first
    assert slugs(second) == [made[2].slug, made[3].slug]
    assert beyond.json()["items"] == [] and beyond.json()["total"] == 5


def test_story_shape_matches_frontend_contract(client, db_session):
    src = f.source(db_session, name="Example Lab Blog")
    ed = f.edition(db_session, slug="2030-w02")
    f.story(
        db_session,
        edition=ed,
        source=src,
        slug="shape-check",
        content="Para one.\n\n\nPara two.",
        original_url="https://lab.example.test/post",
        canonical_url="https://lab.example.test/post",
        image_url="https://img.example.test/a.png",
        image_alt="Alt",
        image_credit="Credit",
        is_featured=True,
    )
    story = client.get("/api/v1/stories/shape-check").json()
    assert story["editionId"] == "2030-w02"
    assert story["sourceName"] == "Example Lab Blog"
    assert story["sourceUrl"] == "https://lab.example.test/post"
    assert story["body"] == ["Para one.", "Para two."]
    assert story["provenance"] == "live"
    assert story["featured"] is True
    assert story["image"] == {"src": "https://img.example.test/a.png", "alt": "Alt", "credit": "Credit"}
    assert set(story["impact"]) == {"whyItMatters", "explore", "buildIdea", "disciplines"}
    date.fromisoformat(story["publishedAt"])  # date-only, like the frontend's ISO dates


def test_get_by_uuid_and_slug(client, db_session):
    s = f.story(db_session, edition=f.edition(db_session), source=f.source(db_session))
    assert client.get(f"/api/v1/stories/{s.slug}").json()["id"] == str(s.id)
    assert client.get(f"/api/v1/stories/{s.id}").json()["slug"] == s.slug
    missing = client.get("/api/v1/stories/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_unpublished_content_is_never_public(client, db_session):
    src, ed = f.source(db_session), f.edition(db_session)
    visible = f.story(db_session, edition=ed, source=src)
    hidden = [
        f.story(db_session, edition=ed, source=src, status="draft", published_at=None),
        f.story(db_session, edition=ed, source=src, status="in_review"),
        f.story(db_session, edition=ed, source=src, status="rejected"),
        f.story(db_session, edition=ed, source=src, status="withdrawn"),
        f.story(db_session, edition=ed, source=src, published_at=f.now() + timedelta(days=1)),  # scheduled
    ]
    draft_edition = f.edition(db_session, status="draft", published_at=None)
    hidden.append(f.story(db_session, edition=draft_edition, source=src))
    future_edition = f.edition(db_session, published_at=f.now() + timedelta(days=2))
    hidden.append(f.story(db_session, edition=future_edition, source=src))

    assert slugs(client.get("/api/v1/stories")) == [visible.slug]
    for s in hidden:
        assert client.get(f"/api/v1/stories/{s.slug}").status_code == 404
        assert client.get(f"/api/v1/stories/{s.id}").status_code == 404
    assert client.get("/api/v1/search", params={"q": "fictional"}).json()["total"] == 1


def test_demo_records_hidden_unless_enabled(client, demo_client, db_session):
    src = f.source(db_session)
    ed = f.edition(db_session, is_demo=True)
    demo = f.story(db_session, edition=ed, source=src, is_demo=True)

    assert client.get("/api/v1/stories").json()["total"] == 0
    assert client.get(f"/api/v1/stories/{demo.slug}").status_code == 404
    shown = demo_client.get(f"/api/v1/stories/{demo.slug}")
    assert shown.status_code == 200 and shown.json()["provenance"] == "sample"


def test_filters(client, db_session):
    src = f.source(db_session)
    a, b = f.edition(db_session, slug="ed-a"), f.edition(db_session, slug="ed-b")
    s1 = f.story(db_session, edition=a, source=src, topic="india", is_featured=True)
    s2 = f.story(db_session, edition=a, source=src, topic="tools")
    s3 = f.story(db_session, edition=b, source=src, topic="tools", published_at=f.now() - timedelta(days=30))

    assert slugs(client.get("/api/v1/stories", params={"topic": "tools"})) == [s2.slug, s3.slug]
    assert set(slugs(client.get("/api/v1/stories", params={"edition": "ed-a"}))) == {s1.slug, s2.slug}
    assert slugs(client.get("/api/v1/stories", params={"featured": "true"})) == [s1.slug]
    old_day = (f.now() - timedelta(days=30)).date().isoformat()
    assert slugs(client.get("/api/v1/stories", params={"from": old_day, "to": old_day})) == [s3.slug]
    assert slugs(client.get("/api/v1/stories", params={"sort": "oldest"}))[0] == s3.slug
    assert client.get("/api/v1/stories", params={"edition": "no-such-edition"}).json()["total"] == 0


def test_keyword_search_ranks_title_matches_first(client, db_session):
    src, ed = f.source(db_session), f.edition(db_session)
    body_hit = f.story(
        db_session, edition=ed, source=src, title="Unrelated", content="We discuss quantisation briefly."
    )
    title_hit = f.story(db_session, edition=ed, source=src, title="Quantisation for laptops")
    f.story(db_session, edition=ed, source=src, title="Nothing relevant here")

    res = client.get("/api/v1/search", params={"q": "quantisation"})
    body = res.json()
    assert body["mode"] == "keyword" and body["query"] == "quantisation"
    assert [s["slug"] for s in body["items"]] == [title_hit.slug, body_hit.slug]
    # Same engine through the list endpoint
    assert set(slugs(client.get("/api/v1/stories", params={"q": "quantisation"}))) == {title_hit.slug, body_hit.slug}


def test_search_tolerates_operator_syntax_and_sql_like_input(client, db_session):
    f.story(db_session, edition=f.edition(db_session), source=f.source(db_session), title="Agents in the browser")
    for q in ['"agents" -browser', "agents OR", "'; DROP TABLE stories; --", "%_\\", "   "]:
        res = client.get("/api/v1/search", params={"q": q})
        assert res.status_code == 200, q
    assert client.get("/api/v1/stories").json()["total"] == 1
