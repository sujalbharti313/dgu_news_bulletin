import pytest
from sqlalchemy import func, select

from app.models import Briefing, Edition, Source, Story, Tool
from app.seed.runner import SeedRefused, check_allowed, run_seed
from scripts import seed as seed_cli
from tests.conftest import make_settings


def test_seed_is_repeatable_and_never_overwrites(db_session):
    first = run_seed(db_session, reference=True, demo=True)
    assert first.inserted["stories"] == 10 and first.inserted["tools"] == 14 and first.inserted["editions"] == 3

    tool = db_session.scalars(select(Tool).where(Tool.slug == "pytorch")).one()
    tool.summary = "Edited by an editor"
    db_session.flush()

    second = run_seed(db_session, reference=True, demo=True)
    assert second.inserted == {}
    assert db_session.scalar(select(func.count()).select_from(Story)) == 10
    db_session.refresh(tool)
    assert tool.summary == "Edited by an editor"


def test_demo_seed_is_flagged_and_attributed_to_a_fictional_source(db_session):
    run_seed(db_session, reference=False, demo=True)
    stories = db_session.scalars(select(Story)).all()
    assert stories and all(s.is_demo and s.original_url is None for s in stories)
    assert all(e.is_demo for e in db_session.scalars(select(Edition)))
    assert all(b.is_demo and b.audio_url is None for b in db_session.scalars(select(Briefing)))
    source = db_session.scalars(select(Source).where(Source.kind == "demo")).one()
    assert source.canonical_domain == "example.invalid" and source.is_active is False


def test_reference_seed_contains_no_demo_rows_or_unverified_prices(db_session):
    run_seed(db_session, reference=True, demo=False)
    assert db_session.scalar(select(func.count()).select_from(Story)) == 0
    tools = db_session.scalars(select(Tool)).all()
    assert all(t.pricing_notes is None and t.last_verified_at is None for t in tools)


def test_demo_content_is_hidden_from_the_public_api_by_default(client, demo_client, db_session):
    run_seed(db_session, reference=True, demo=True)
    assert client.get("/api/v1/stories").json()["total"] == 0
    assert client.get("/api/v1/editions/current").status_code == 404
    assert client.get("/api/v1/tools").json()["total"] == 14  # reference data is real and public
    current = demo_client.get("/api/v1/editions/current").json()
    assert current["id"] == "2026-w43" and current["provenance"] == "sample"
    # Same order the sample adapter renders: featured first, then newest.
    assert [st["slug"] for st in current["stories"]] == [
        "small-open-models-on-a-laptop",
        "reading-a-model-card",
        "indian-language-speech-datasets",
        "agents-that-use-a-browser",
    ]


@pytest.mark.parametrize(
    ("env", "kwargs", "allowed"),
    [
        ("development", {"reference": True, "demo": True, "confirm_production": False}, True),
        ("production", {"reference": False, "demo": True, "confirm_production": True}, False),
        ("production", {"reference": True, "demo": False, "confirm_production": False}, False),
        ("production", {"reference": True, "demo": False, "confirm_production": True}, True),
        ("development", {"reference": False, "demo": False, "confirm_production": False}, False),
    ],
)
def test_seed_guards(env, kwargs, allowed):
    settings = make_settings(app_env=env, database_url="postgresql://u:p@db.example.test/postgres")
    if allowed:
        check_allowed(settings, **kwargs)
    else:
        with pytest.raises(SeedRefused):
            check_allowed(settings, **kwargs)


def test_cli_refuses_demo_in_production_before_connecting(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/never_used")
    assert seed_cli.main(["--demo"]) == 2
    assert "never seeded" in capsys.readouterr().err
