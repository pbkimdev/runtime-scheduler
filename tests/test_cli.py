from __future__ import annotations

from dataclasses import dataclass

import pytest

from scheduler.cli import ledger_commit_reason
from scheduler.ledger import MonthLedger
from scheduler.reconcile import STATUS_OK, SourceResult


@dataclass
class StubFacts:
    ledger: MonthLedger
    jobs_added: int = 0


def facts_for(loaded: str, cursor: str, jobs_added: int = 0) -> StubFacts:
    ledger = MonthLedger(month="2026-09", cursor=loaded)
    ledger.cursor = cursor
    return StubFacts(ledger=ledger, jobs_added=jobs_added)


@pytest.mark.parametrize(
    ("loaded", "cursor", "jobs_added", "results", "expected"),
    [
        # A tick that saw work always commits.
        ("2026-09-11T17:00:00Z", "2026-09-11T17:59:00Z", 3, [], "jobs"),
        # So does one that reconciled, because the ledger gained a comparison.
        (
            "2026-09-11T17:00:00Z",
            "2026-09-11T17:59:00Z",
            0,
            [SourceResult(source="github_org", status=STATUS_OK)],
            "reconciliation",
        ),
        # A quiet tick inside the heartbeat leaves the file alone, so the
        # workflow finds a clean git status and commits nothing.
        ("2026-09-11T17:30:00Z", "2026-09-11T17:59:00Z", 0, [], "none"),
        ("2026-09-11T17:00:00Z", "2026-09-11T18:00:00Z", 0, [], "none"),
        # Past the heartbeat it writes, which is what keeps the repository
        # active for the 60-day scheduled-workflow rule.
        ("2026-09-11T16:59:00Z", "2026-09-11T18:00:00Z", 0, [], "heartbeat"),
    ],
)
def test_ledger_commit_cadence(loaded, cursor, jobs_added, results, expected):
    facts = facts_for(loaded, cursor, jobs_added)
    assert ledger_commit_reason(facts, results, 60) == expected


def test_an_unreadable_cursor_always_commits():
    facts = facts_for("2026-09-11T17:00:00Z", "2026-09-11T17:10:00Z")
    facts.ledger.loaded_cursor = ""
    facts.ledger.cursor = ""
    assert ledger_commit_reason(facts, [], 60) == "heartbeat"


def test_a_fresh_month_file_records_the_cursor_it_loaded():
    ledger = MonthLedger(month="2026-09", cursor="2026-09-01T00:00:00Z")
    assert ledger.loaded_cursor == "2026-09-01T00:00:00Z"
    ledger.advance_cursor(60, scanned_through=None)
    assert ledger.loaded_cursor == "2026-09-01T00:00:00Z"
