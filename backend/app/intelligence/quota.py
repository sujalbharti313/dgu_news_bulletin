"""Free-tier usage controls: a daily usage ledger shared by every process, plus in-process pacing.

- `UsageLedger` counts requests and tokens per model per *quota day*. Gemini's daily free quotas reset at
  midnight Pacific time, so the quota day is the date in America/Los_Angeles. Before every request the engine
  *reserves* one request atomically; when AI_REQUESTS_PER_DAY or AI_TOKENS_PER_DAY would be crossed, the
  reservation fails and the run stops before Google has to refuse it.
- When Google does answer with a daily-quota 429, the ledger records `quota_exhausted_until` (the next reset),
  and every later run in any process stops at once instead of sending more requests.
- `RequestPacer` spaces request starts under AI_REQUESTS_PER_MINUTE; `RateGate` makes every worker pause
  together after a per-minute 429.

The ledger holds counts only: no prompts, no article text, no keys.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import and_, func, select, text, true
from sqlalchemy.dialects.postgresql import insert

from app.db.session import Database
from app.models.intelligence import AiUsageDaily

try:  # tzdata ships with Linux; on Windows it comes from the tzdata package in requirements.txt
    from zoneinfo import ZoneInfo

    QUOTA_TZ: timezone | ZoneInfo = ZoneInfo("America/Los_Angeles")
except Exception:  # noqa: BLE001 - fall back to Pacific Standard Time (an hour early in summer, never late)
    QUOTA_TZ = timezone(timedelta(hours=-8))

PROVIDER = "gemini"


def quota_day(now: datetime) -> date:
    return now.astimezone(QUOTA_TZ).date()


def next_reset(now: datetime) -> datetime:
    """The next midnight Pacific time, in UTC."""
    local = now.astimezone(QUOTA_TZ)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=QUOTA_TZ)
    return midnight.astimezone(UTC)


@dataclass
class UsageSnapshot:
    day: date
    model: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rate_limited: int = 0
    failed_requests: int = 0
    quota_exhausted_until: datetime | None = None

    def as_dict(self, *, requests_per_day: int, tokens_per_day: int) -> dict:
        return {
            "quotaDay": self.day.isoformat(),
            "model": self.model,
            "requests": self.requests,
            "requestsPerDayLimit": requests_per_day or None,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "tokensPerDayLimit": tokens_per_day or None,
            "rateLimitedResponses": self.rate_limited,
            "failedRequests": self.failed_requests,
            "quotaExhaustedUntil": self.quota_exhausted_until.isoformat() if self.quota_exhausted_until else None,
        }


class UsageLedger(Protocol):
    def reserve(self, model: str, now: datetime, *, requests_per_day: int, tokens_per_day: int) -> bool: ...
    def record(self, model: str, now: datetime, *, input_tokens: int, output_tokens: int, outcome: str) -> None: ...
    def mark_exhausted(self, model: str, now: datetime, until: datetime) -> None: ...
    def snapshot(self, model: str, now: datetime) -> UsageSnapshot: ...


class SqlUsageLedger:
    def __init__(self, db: Database):
        self.db = db

    def check_schema(self) -> bool:
        with self.db.write_session() as s:
            return bool(s.scalar(text("SELECT to_regclass('ai_usage_daily') IS NOT NULL")))

    def reserve(self, model, now, *, requests_per_day, tokens_per_day):
        t = AiUsageDaily.__table__
        conditions = []
        if requests_per_day:
            conditions.append(t.c.requests < requests_per_day)
        if tokens_per_day:
            conditions.append((t.c.input_tokens + t.c.output_tokens) < tokens_per_day)
        stmt = insert(AiUsageDaily).values(usage_date=quota_day(now), provider=PROVIDER, model=model, requests=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[t.c.usage_date, t.c.provider, t.c.model],
            set_={"requests": t.c.requests + 1, "updated_at": func.now()},
            where=and_(true(), *conditions),
        ).returning(t.c.requests)
        with self.db.write_session() as s:
            return s.execute(stmt).first() is not None

    def record(self, model, now, *, input_tokens, output_tokens, outcome):
        t = AiUsageDaily.__table__
        values = {
            "input_tokens": t.c.input_tokens + max(0, input_tokens),
            "output_tokens": t.c.output_tokens + max(0, output_tokens),
            "updated_at": func.now(),
        }
        if outcome == "rate_limited":
            values["rate_limited"] = t.c.rate_limited + 1
        elif outcome != "ok":
            values["failed_requests"] = t.c.failed_requests + 1
        with self.db.write_session() as s:
            s.execute(
                t.update()
                .where(t.c.usage_date == quota_day(now), t.c.provider == PROVIDER, t.c.model == model)
                .values(**values)
            )

    def mark_exhausted(self, model, now, until):
        t = AiUsageDaily.__table__
        stmt = insert(AiUsageDaily).values(
            usage_date=quota_day(now), provider=PROVIDER, model=model, quota_exhausted_until=until
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[t.c.usage_date, t.c.provider, t.c.model],
            set_={"quota_exhausted_until": until, "updated_at": func.now()},
        )
        with self.db.write_session() as s:
            s.execute(stmt)

    def snapshot(self, model, now):
        with self.db.write_session() as s:
            row = s.scalar(
                select(AiUsageDaily).where(
                    AiUsageDaily.usage_date == quota_day(now),
                    AiUsageDaily.provider == PROVIDER,
                    AiUsageDaily.model == model,
                )
            )
            # A quota exhaustion recorded yesterday can still be in force (e.g. recorded 23:59 Pacific).
            latest = s.scalar(
                select(func.max(AiUsageDaily.quota_exhausted_until)).where(
                    AiUsageDaily.provider == PROVIDER, AiUsageDaily.model == model
                )
            )
        snap = UsageSnapshot(day=quota_day(now), model=model)
        if row is not None:
            snap = UsageSnapshot(
                day=row.usage_date,
                model=model,
                requests=row.requests,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                rate_limited=row.rate_limited,
                failed_requests=row.failed_requests,
            )
        snap.quota_exhausted_until = latest if latest is not None and latest > now else None
        return snap


class MemoryUsageLedger:
    """Same behaviour in memory (tests; dry runs without the migration)."""

    def __init__(self):
        self.rows: dict[tuple[date, str], UsageSnapshot] = {}

    def _row(self, model: str, now: datetime) -> UsageSnapshot:
        return self.rows.setdefault((quota_day(now), model), UsageSnapshot(day=quota_day(now), model=model))

    def reserve(self, model, now, *, requests_per_day, tokens_per_day):
        row = self._row(model, now)
        if requests_per_day and row.requests >= requests_per_day:
            return False
        if tokens_per_day and row.input_tokens + row.output_tokens >= tokens_per_day:
            return False
        row.requests += 1
        return True

    def record(self, model, now, *, input_tokens, output_tokens, outcome):
        row = self._row(model, now)
        row.input_tokens += max(0, input_tokens)
        row.output_tokens += max(0, output_tokens)
        if outcome == "rate_limited":
            row.rate_limited += 1
        elif outcome != "ok":
            row.failed_requests += 1

    def mark_exhausted(self, model, now, until):
        self._row(model, now).quota_exhausted_until = until

    def snapshot(self, model, now):
        row = self._row(model, now)
        latest = max(
            (r.quota_exhausted_until for (_, m), r in self.rows.items() if m == model and r.quota_exhausted_until),
            default=None,
        )
        return UsageSnapshot(
            day=row.day,
            model=model,
            requests=row.requests,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            rate_limited=row.rate_limited,
            failed_requests=row.failed_requests,
            quota_exhausted_until=latest if latest is not None and latest > now else None,
        )


class RateGate:
    """Shared pause for every worker after a rate-limit response, so concurrent calls back off together."""

    def __init__(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock=time.monotonic):
        self._until = 0.0
        self._sleep = sleep
        self._clock = clock

    def block(self, seconds: float) -> None:
        self._until = max(self._until, self._clock() + seconds)

    async def wait(self) -> None:
        delay = self._until - self._clock()
        if delay > 0:
            await self._sleep(delay)


class RequestPacer:
    """Spaces request starts so a run stays under a requests-per-minute limit, across every worker.
    Retries and repair calls go through it too: each is a request against the quota."""

    def __init__(
        self, per_minute: int, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock=time.monotonic
    ):
        self._interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._next = 0.0
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            delay = self._next - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next = max(self._clock(), self._next) + self._interval
