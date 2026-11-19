"""The weekly edition window: Monday 00:00 to Sunday 23:59:59.999999 in Asia/Kolkata, as UTC instants.

All date arithmetic for Stage 3.4 goes through here, so the bulletin's week is defined in one place."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo

try:  # tzdata ships with Linux; on Windows it comes from the tzdata package in requirements.txt
    from zoneinfo import ZoneInfo

    def _zone(name: str) -> tzinfo:
        return ZoneInfo(name)
except Exception:  # noqa: BLE001 - India has no daylight saving time, so a fixed offset is exact

    def _zone(name: str) -> tzinfo:
        return timezone(timedelta(hours=5, minutes=30))


IST_NAME = "Asia/Kolkata"


def edition_zone(name: str = IST_NAME) -> tzinfo:
    try:
        return _zone(name)
    except Exception:  # noqa: BLE001 - unknown zone name: India Standard Time (the bulletin's only audience)
        return timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class EditionWindow:
    start_date: date  # a Monday, in the edition time zone
    timezone_name: str = IST_NAME

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=6)

    @property
    def tz(self) -> tzinfo:
        return edition_zone(self.timezone_name)

    @property
    def start(self) -> datetime:
        return datetime.combine(self.start_date, time.min, tzinfo=self.tz).astimezone(UTC)

    @property
    def end(self) -> datetime:
        """Exclusive end: the next Monday 00:00 local time."""
        return datetime.combine(self.start_date + timedelta(days=7), time.min, tzinfo=self.tz).astimezone(UTC)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def local_date(self, moment: datetime) -> date:
        return moment.astimezone(self.tz).date()

    @property
    def label(self) -> str:
        return f"{self.start_date.isoformat()} to {self.end_date.isoformat()} ({self.timezone_name})"

    def as_dict(self) -> dict:
        return {
            "startDate": self.start_date.isoformat(),
            "endDate": self.end_date.isoformat(),
            "timezone": self.timezone_name,
            "startsAt": self.start.isoformat(),
            "endsBefore": self.end.isoformat(),
        }


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def edition_for(now: datetime, *, week: str = "previous", timezone_name: str = IST_NAME) -> EditionWindow:
    """`previous` = the last complete Monday-Sunday week (what a Monday edition covers); `current` = the week
    containing `now` (a preview, still being collected); or an ISO date inside the wanted week."""
    local_today = now.astimezone(edition_zone(timezone_name)).date()
    if week == "previous":
        start = monday_of(local_today) - timedelta(days=7)
    elif week == "current":
        start = monday_of(local_today)
    else:
        try:
            start = monday_of(date.fromisoformat(week))
        except ValueError as exc:
            raise ValueError("week must be 'previous', 'current' or a date (YYYY-MM-DD)") from exc
    return EditionWindow(start, timezone_name)
