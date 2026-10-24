from datetime import date, timedelta

from tests import factories as f


def test_editions_newest_week_first_and_drafts_hidden(client, db_session):
    old = f.edition(db_session, week_start=date(2030, 3, 4))
    new = f.edition(db_session, week_start=date(2030, 3, 11))
    f.edition(db_session, week_start=date(2030, 3, 18), status="draft", published_at=None)
    f.edition(db_session, week_start=date(2030, 3, 25), published_at=f.now() + timedelta(days=1))

    body = client.get("/api/v1/editions").json()
    assert [e["id"] for e in body["items"]] == [new.slug, old.slug]
    assert body["items"][0]["weekOf"] == "2030-03-11"
    assert body["items"][0]["provenance"] == "live"


def test_current_edition_uses_latest_public_week_and_orders_stories(client, db_session):
    src = f.source(db_session)
    f.edition(db_session, week_start=date(2030, 4, 1))
    current = f.edition(db_session, week_start=date(2030, 4, 8), slug="2030-w15")
    f.edition(db_session, week_start=date(2030, 4, 15), status="draft", published_at=None)
    second = f.story(
        db_session, edition=current, source=src, edition_position=2, published_at=f.now() - timedelta(hours=1)
    )
    first = f.story(
        db_session, edition=current, source=src, edition_position=1, published_at=f.now() - timedelta(hours=5)
    )
    featured = f.story(
        db_session, edition=current, source=src, is_featured=True, published_at=f.now() - timedelta(days=2)
    )
    f.story(db_session, edition=current, source=src, status="draft", published_at=None)

    res = client.get("/api/v1/editions/current")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "2030-w15"
    assert [s["slug"] for s in body["stories"]] == [featured.slug, first.slug, second.slug]


def test_current_edition_404_when_nothing_published(client, db_session):
    f.edition(db_session, status="draft", published_at=None)
    res = client.get("/api/v1/editions/current")
    assert res.status_code == 404
    assert res.json()["error"]["message"] == "No editions have been published yet."


def test_edition_by_slug(client, db_session):
    ed = f.edition(db_session, slug="2030-w20")
    draft = f.edition(db_session, slug="2030-w21", status="draft", published_at=None)
    assert client.get("/api/v1/editions/2030-w20").json()["number"] == ed.number
    assert client.get(f"/api/v1/editions/{draft.slug}").status_code == 404


def test_latest_briefing(client, db_session):
    older = f.edition(db_session, week_start=date(2030, 5, 6))
    newer = f.edition(db_session, week_start=date(2030, 5, 13), slug="2030-w20b")
    unpublished_week = f.edition(db_session, week_start=date(2030, 5, 20), status="draft", published_at=None)
    f.briefing(db_session, edition=older, title="Older")
    f.briefing(db_session, edition=newer, title="Newer")
    f.briefing(db_session, edition=unpublished_week, title="Hidden")

    body = client.get("/api/v1/briefings/latest").json()
    assert body == {
        "editionId": "2030-w20b",
        "title": "Newer",
        "audioUrl": None,
        "durationSeconds": None,
        "transcript": ["Line one.", "Line two."],
        "provenance": "live",
    }


def test_latest_briefing_404_when_none(client):
    assert client.get("/api/v1/briefings/latest").status_code == 404
