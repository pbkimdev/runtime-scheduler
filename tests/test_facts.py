from __future__ import annotations

import pytest

from scheduler import facts as facts_mod


@pytest.mark.parametrize(
    ("age_s", "expected"),
    [
        (0, facts_mod.NORMAL),
        (29 * 60, facts_mod.NORMAL),
        (30 * 60, facts_mod.DEGRADED),
        (23 * 3600, facts_mod.DEGRADED),
        (24 * 3600, facts_mod.DEGRADED),
        (24 * 3600 + 1, facts_mod.STALE),
    ],
)
def test_staleness_thresholds(policy, age_s, expected):
    assert facts_mod.staleness_for(age_s, policy) == expected


def test_trailing_read_failures_are_consecutive_only():
    reads = [
        {"github": False, "archbox": True},
        {"github": True, "archbox": False},
    ]
    assert facts_mod._trailing_failures(reads, "github") == 0
    assert facts_mod._trailing_failures(reads, "archbox") == 1
    reads = [{"github": False}, {"github": False}]
    assert facts_mod._trailing_failures(reads, "github") == 2
