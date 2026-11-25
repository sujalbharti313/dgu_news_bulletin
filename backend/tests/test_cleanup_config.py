"""Stage 3.6 retention configuration, the scheduler wiring and the CLI (no database needed for most)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from app.cleanup import build_cleanup_service
from app.cleanup.config import RetentionConfig, RetentionConfigError, load_retention_config
from app.ranking.config import load_ranking_config
from app.ranking.window import EditionWindow
from tests.conftest import BACKEND_DIR, make_settings

ROOT = Path(BACKEND_DIR).parent


def test_shipped_policy_is_valid_and_outlives_the_semantic_lookback():
    lookback = load_ranking_config("config/ranking.toml").semantic.lookback_days
    config = load_retention_config("config/retention.toml", semantic_lookback_days=lookback)
    assert config.enabled
    assert config.published_week.min_story_age_days > lookback
    assert config.orphans.min_age_days > lookback
    assert config.published_week.safety_delay_hours >= 24
    assert len(config.version) == 16


def test_policy_shorter_than_the_lookback_is_refused(tmp_path):
    f = tmp_path / "r.toml"
    f.write_text("[published_week]\nmin_story_age_days = 14\n")
    with pytest.raises(RetentionConfigError, match="semantic lookback"):
        load_retention_config(f, semantic_lookback_days=28)


@pytest.mark.parametrize(
    "body",
    [
        "unknown = 1\n",
        "[published_week]\nsafety_delay_hours = 1\n",  # below the 24 h minimum
        "[execution]\nbatch_size = 0\n",
        "[execution]\nmax_retries = -1\n",
        "not toml = = 1",
    ],
)
def test_invalid_policies_are_refused(tmp_path, body):
    f = tmp_path / "r.toml"
    f.write_text(body)
    with pytest.raises(RetentionConfigError):
        load_retention_config(f)


def test_missing_policy_file_is_a_clear_error(tmp_path):
    with pytest.raises(RetentionConfigError, match="not found"):
        load_retention_config(tmp_path / "missing.toml")


def test_policy_version_changes_with_the_values():
    a = RetentionConfig()
    b = RetentionConfig.model_validate({"published_week": {"safety_delay_hours": 96}})
    assert a.version != b.version and a.version == RetentionConfig().version


def test_service_wiring_uses_the_configured_files():
    service = build_cleanup_service(make_settings(), db=None)
    assert service.config.version == load_retention_config("config/retention.toml").version
    assert service.start_week is not None and service.tz_name == "Asia/Kolkata"


def test_week_deadlines_are_computed_in_the_bulletin_time_zone():
    window = EditionWindow(datetime(2026, 11, 16).date(), "Asia/Kolkata")
    # Monday 23 Nov 00:00 IST = Sunday 22 Nov 18:30 UTC.
    assert window.end == datetime(2026, 11, 22, 18, 30, tzinfo=UTC)
    service = build_cleanup_service(make_settings(), db=None)
    published = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)
    # The first edition (16-22 Nov, published 25 Nov): earliest deletion is the week end + 35 days.
    assert service._not_before(published, window) == datetime(2026, 12, 27, 18, 30, tzinfo=UTC)


def test_scheduler_runs_cleanup_last_without_ai_and_on_free_infrastructure():
    wf = yaml.safe_load((ROOT / ".github/workflows/weekly-pipeline.yml").read_text())
    steps = wf["jobs"]["pipeline"]["steps"]
    names = [s.get("name") or "" for s in steps]
    cleanup = names.index("Stage 3.6: cleanup and data retention")
    assert cleanup == len(names) - 2  # after the whole news pipeline, before the report
    assert "python -m scripts.cleanup scheduled" in steps[cleanup]["run"]
    assert wf["jobs"]["pipeline"]["runs-on"] == "ubuntu-latest"
    assert wf["concurrency"]["cancel-in-progress"] is False  # never two pipeline runs at once
    assert "cleanup" in wf[True]["workflow_dispatch"]["inputs"]["steps"]["options"]


def test_cleanup_never_imports_an_ai_provider():
    source = "".join(p.read_text() for p in (Path(BACKEND_DIR) / "app" / "cleanup").glob("*.py"))
    for forbidden in ("genai", "GeminiProvider", "providers", "httpx", "compose"):
        assert forbidden not in source


def test_cli_check_works_without_a_database(capsys, monkeypatch):
    from scripts import cleanup as cli

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "Settings", lambda: make_settings(database_url=None))
    assert cli.main(["check"]) == 0
    assert "Retention configuration OK" in capsys.readouterr().out
