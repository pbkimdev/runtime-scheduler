from __future__ import annotations

import copy
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scheduler.ledger import JobRecord, MonthLedger, format_ts
from scheduler.policy import parse_policy

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def _raw_policy() -> dict:
    return tomllib.loads((ROOT / "policy.toml").read_text(encoding="utf-8"))


@pytest.fixture
def raw_policy() -> dict:
    return _raw_policy()


@pytest.fixture
def policy():
    """The shipped policy, exactly as the allocator reads it in production."""
    return parse_policy(_raw_policy())


@pytest.fixture
def plan_policy():
    """The plan's worked example: all three providers live, GitHub reserve at
    the calibrated 0.10 rather than the E9b placeholder."""
    raw = _raw_policy()
    raw["reserves"]["github"] = 0.10
    for provider in ("blacksmith", "archbox"):
        raw["providers"][provider]["live"] = True
    return parse_policy(raw)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def make_record(**overrides) -> JobRecord:
    base = dict(
        key="1:1:1",
        repo="pbkimdev/mantra",
        private=True,
        name="linux / primary",
        labels=("ubuntu-latest",),
        provider="github",
        family="linux",
        routed=True,
        event="push",
        created_at="2026-09-11T17:00:00Z",
        started_at="2026-09-11T17:00:10Z",
        completed_at="2026-09-11T17:05:00Z",
        conclusion="success",
        work="success",
        infra=False,
        native=5.0,
        normalized=10.0,
        latency_s=10.0,
    )
    base.update(overrides)
    base["labels"] = tuple(base["labels"])
    return JobRecord(**base)


def ledger_with(records, cursor: datetime, month: str = "2026-09") -> MonthLedger:
    ledger = MonthLedger(month=month, cursor=format_ts(cursor))
    for record in records:
        ledger.jobs[record.key] = record
    return ledger


def load_fixture(name: str) -> dict:
    import json

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def deep(value):
    return copy.deepcopy(value)
