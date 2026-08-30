"""Monthly pacing. Every boundary is UTC because vendor allowances reset on
calendar months and the allocator's clock is a GitHub runner's."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def month_bounds(t: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the calendar month containing t, in UTC."""
    t = t.astimezone(UTC)
    start = datetime(t.year, t.month, 1, tzinfo=UTC)
    if t.month == 12:
        end = datetime(t.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(t.year, t.month + 1, 1, tzinfo=UTC)
    return start, end


def month_key(t: datetime) -> str:
    t = t.astimezone(UTC)
    return f"{t.year:04d}-{t.month:02d}"


def elapsed_fraction(t: datetime) -> float:
    start, end = month_bounds(t)
    span = (end - start).total_seconds()
    return (t.astimezone(UTC) - start).total_seconds() / span


def pace(t: datetime, slack: float) -> float:
    return min(1.0, elapsed_fraction(t) + slack)


def cap_hard(allowance: float, hard_reserve: float) -> float:
    return allowance * (1.0 - hard_reserve)


def cap_pace(allowance: float, hard_reserve: float, t: datetime, slack: float) -> float:
    return cap_hard(allowance, hard_reserve) * pace(t, slack)


def epoch(minutes: int) -> timedelta:
    return timedelta(minutes=minutes)
