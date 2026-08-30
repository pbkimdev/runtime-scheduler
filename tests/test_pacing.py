from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scheduler import pacing
from tests.conftest import at


def test_month_bounds_roll_into_january():
    start, end = pacing.month_bounds(at("2026-12-17T09:00:00Z"))
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_month_bounds_use_utc_not_local_time():
    # 00:30 in Seoul on the first is still the previous month in UTC.
    start, _ = pacing.month_bounds(datetime.fromisoformat("2026-09-01T00:30:00+09:00"))
    assert start == datetime(2026, 8, 1, tzinfo=UTC)


def test_worked_example_pace():
    # 2026-09-11T18:00Z is 10.75 days into a 30-day month.
    assert pacing.elapsed_fraction(at("2026-09-11T18:00:00Z")) == pytest.approx(
        0.35833, abs=1e-5
    )
    assert pacing.pace(at("2026-09-11T18:00:00Z"), 0.15) == pytest.approx(
        0.50833, abs=1e-5
    )


def test_pace_is_capped_at_one():
    assert pacing.pace(at("2026-09-30T23:59:00Z"), 0.15) == 1.0


def test_worked_example_caps():
    now = at("2026-09-11T18:00:00Z")
    assert pacing.cap_hard(2000, 0.10) == 1800
    assert pacing.cap_pace(2000, 0.10, now, 0.15) == pytest.approx(915.0, abs=0.1)
    assert pacing.cap_hard(3000, 0.20) == 2400
    assert pacing.cap_pace(3000, 0.20, now, 0.15) == pytest.approx(1220.0, abs=0.1)
