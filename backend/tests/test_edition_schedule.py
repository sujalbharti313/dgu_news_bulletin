"""Stage 3.5 weekly schedule: Monday-Sunday windows in Asia/Kolkata stored as UTC, due times, catch-up after missed
runs, the start week, and refusal to generate a week that has not ended."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.editions.config import EditionConfigError, Schedule, load_edition_config
from app.editions.schedule import due_at, due_weeks, edition_slug, last_complete_week, week_for
from app.ranking.window import EditionWindow

IST = Schedule(timezone="Asia/Kolkata", generation_weekday=0, generation_time="06:00", catch_up_weeks=1)
WEEK = EditionWindow(date(2026, 11, 16))  # Monday 16 to Sunday 22 November 2026


def ist(y, m, d, hh=0, mm=0, ss=0, us=0) -> datetime:
    """An Asia/Kolkata wall-clock time as a UTC instant (IST = UTC+05:30, no daylight saving)."""
    return datetime(y, m, d, hh, mm, ss, us, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def test_window_is_monday_to_sunday_local_time_stored_as_utc():
    assert WEEK.start_date.weekday() == 0 and WEEK.end_date == date(2026, 11, 22)
    assert WEEK.start == datetime(2026, 11, 15, 18, 30, tzinfo=UTC)  # Monday 00:00 IST
    assert WEEK.end == datetime(2026, 11, 22, 18, 30, tzinfo=UTC)  # next Monday 00:00 IST (exclusive)


def test_monday_and_sunday_news_are_included_and_next_week_is_not():
    assert WEEK.contains(ist(2026, 11, 16, 0, 0))  # the first instant of Monday
    assert WEEK.contains(ist(2026, 11, 16, 9, 15))
    assert WEEK.contains(ist(2026, 11, 22, 23, 59, 59, 999999))  # the last instant of Sunday
    assert not WEEK.contains(ist(2026, 11, 23, 0, 0))  # next Monday belongs to the next edition
    assert not WEEK.contains(ist(2026, 11, 15, 23, 59, 59))  # the previous Sunday


def test_utc_instants_near_midnight_are_interpreted_in_india_time():
    # 20:00 UTC on Sunday 22 Nov is already Monday 01:30 in India: next week.
    assert not WEEK.contains(datetime(2026, 11, 22, 20, 0, tzinfo=UTC))
    # 19:00 UTC on Sunday 15 Nov is Monday 00:30 in India: this week.
    assert WEEK.contains(datetime(2026, 11, 15, 19, 0, tzinfo=UTC))


def test_edition_is_due_monday_six_am_local_after_the_week():
    assert due_at(WEEK, IST) == ist(2026, 11, 23, 6, 0)
    assert due_weeks(ist(2026, 11, 23, 5, 59), IST) == [EditionWindow(date(2026, 11, 9))]  # catch-up only
    weeks = due_weeks(ist(2026, 11, 23, 6, 0), IST)
    assert weeks[0] == WEEK and weeks[1].start_date == date(2026, 11, 9)


def test_nothing_is_due_for_the_running_week():
    for moment in (ist(2026, 11, 16, 0, 1), ist(2026, 11, 20, 12), ist(2026, 11, 22, 23, 59)):
        assert WEEK not in due_weeks(moment, IST)
    with pytest.raises(ValueError, match="not ended"):
        week_for("2026-11-18", ist(2026, 11, 20, 12), IST)


def test_missed_runs_are_recovered_by_catch_up():
    # The Monday run was missed; on Wednesday the same week is still due (idempotency prevents duplicates).
    assert due_weeks(ist(2026, 11, 25, 10), IST)[0] == WEEK
    # Missed for a whole week: with catch_up_weeks = 1 the older week is still due on the next Monday.
    weeks = due_weeks(ist(2026, 11, 30, 7), IST)
    assert [w.start_date for w in weeks] == [date(2026, 11, 23), date(2026, 11, 16)]
    no_catch_up = IST.model_copy(update={"catch_up_weeks": 0})
    assert [w.start_date for w in due_weeks(ist(2026, 11, 30, 7), no_catch_up)] == [date(2026, 11, 23)]


def test_start_week_prevents_back_filling():
    schedule = IST.model_copy(update={"start_week": date(2026, 11, 16)})
    assert [w.start_date for w in due_weeks(ist(2026, 11, 23, 7), schedule)] == [date(2026, 11, 16)]
    with pytest.raises(ValueError, match="start_week"):
        week_for("2026-11-10", ist(2026, 11, 23, 7), schedule)


def test_week_for_and_last_complete_week():
    now = ist(2026, 11, 23, 7)
    assert last_complete_week(now, IST) == WEEK
    assert week_for("previous", now, IST) == WEEK
    assert week_for("2026-11-22", now, IST) == WEEK  # a Sunday maps to its own Monday
    with pytest.raises(ValueError):
        week_for("next tuesday", now, IST)


def test_week_identifier_is_the_iso_week():
    assert edition_slug(WEEK) == "2026-w47"
    assert edition_slug(EditionWindow(date(2026, 12, 28))) == "2026-w53"
    assert edition_slug(EditionWindow(date(2027, 1, 4))) == "2027-w01"


def test_other_generation_day_and_time():
    tuesday_noon = IST.model_copy(update={"generation_weekday": 1, "generation_time": "12:30"})
    assert due_at(WEEK, tuesday_noon) == ist(2026, 11, 24, 12, 30)


def test_daylight_saving_zone_still_uses_local_midnights():
    # The schedule is zone-aware; with a DST zone the window follows local midnight across the change.
    london = EditionWindow(date(2026, 10, 19), "Europe/London")  # DST ends Sunday 25 October 2026
    assert london.start == datetime(2026, 10, 18, 23, 0, tzinfo=UTC)  # BST (UTC+1)
    assert london.end == datetime(2026, 10, 26, 0, 0, tzinfo=UTC)  # GMT (UTC+0)


def test_shipped_config_is_valid_and_rejects_bad_values(tmp_path):
    config = load_edition_config("config/edition.toml")
    assert config.schedule.timezone == "Asia/Kolkata" and config.selection.target_stories == 10
    assert config.schedule.start_week.weekday() == 0
    bad = tmp_path / "bad.toml"
    bad.write_text("[schedule]\nstart_week = 2026-11-17\n[selection]\ntarget_stories = 0\n")
    with pytest.raises(EditionConfigError, match="start_week|target_stories"):
        load_edition_config(bad)
    bad.write_text('[schedule]\ntimezone = "Mars/Olympus"\n')
    with pytest.raises(EditionConfigError, match="time zone"):
        load_edition_config(bad)
