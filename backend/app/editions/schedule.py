"""When an edition is due. All week arithmetic reuses Stage 3.4's EditionWindow (Monday 00:00 to the next
Monday 00:00 in the bulletin's time zone, as UTC instants), so both stages agree on what "a week" is.

A week's edition becomes due on `generation_weekday` at `generation_time` (local) after the week has ended:
by default Monday 06:00 Asia/Kolkata for the Monday-Sunday week before. Nothing is ever due for a week that is
still being collected. The scheduler may run as often as it likes; it only acts on due weeks without an edition.
"""

from datetime import UTC, datetime, timedelta

from app.editions.config import Schedule
from app.ranking.window import EditionWindow, monday_of


def due_at(window: EditionWindow, schedule: Schedule) -> datetime:
    """The UTC instant from which the edition for `window` may be generated."""
    day = window.start_date + timedelta(days=7 + schedule.generation_weekday)
    local = datetime.combine(day, schedule.local_time, tzinfo=schedule.tz)
    return max(local.astimezone(UTC), window.end)


def last_complete_week(now: datetime, schedule: Schedule) -> EditionWindow:
    local_today = now.astimezone(schedule.tz).date()
    return EditionWindow(monday_of(local_today) - timedelta(days=7), schedule.timezone)


def due_weeks(now: datetime, schedule: Schedule) -> list[EditionWindow]:
    """Weeks whose edition is due at `now`, newest first: the last complete week (once due) plus up to
    `catch_up_weeks` earlier weeks, so a missed scheduler run is recovered on the next one."""
    latest = last_complete_week(now, schedule)
    weeks = []
    for back in range(schedule.catch_up_weeks + 1):
        window = EditionWindow(latest.start_date - timedelta(days=7 * back), schedule.timezone)
        if schedule.start_week and window.start_date < schedule.start_week:
            continue
        if now >= due_at(window, schedule) and now >= window.end:
            weeks.append(window)
    return weeks


def week_for(value: str, now: datetime, schedule: Schedule) -> EditionWindow:
    """`previous` = the last complete week; or an ISO date inside the wanted week. The current, still running
    week is refused: an edition must never be generated before its collection window has ended."""
    if value == "previous":
        window = last_complete_week(now, schedule)
    else:
        try:
            day = datetime.fromisoformat(value).date()
        except ValueError as exc:
            raise ValueError("week must be 'previous' or a date (YYYY-MM-DD)") from exc
        window = EditionWindow(monday_of(day), schedule.timezone)
    if now < window.end:
        raise ValueError(f"The week {window.label} has not ended yet; its edition cannot be generated.")
    if schedule.start_week and window.start_date < schedule.start_week:
        raise ValueError(f"Editions start with the week of {schedule.start_week.isoformat()} (config start_week).")
    return window


def edition_slug(window: EditionWindow) -> str:
    year, week, _ = window.start_date.isocalendar()
    return f"{year}-w{week:02d}"
