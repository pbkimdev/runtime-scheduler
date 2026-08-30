from __future__ import annotations

import json

import pytest

from scheduler import facts as facts_mod
from scheduler.github import GitHubError
from scheduler.ledger import MonthLedger
from tests.conftest import at


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


class FakeClient:
    """Enough of the REST surface for one tick, with the scan window recorded."""

    def __init__(self, *, state=None, repos=None, fail_repos=False):
        self.state = state
        self.repos = repos if repos is not None else []
        self.fail_repos = fail_repos
        self.created_since: list[str] = []
        self.listed: list[str] = []

    def get_org_variable(self, org, name):
        return None if self.state is None else json.dumps(self.state)

    def list_org_repos(self, org):
        if self.fail_repos:
            raise GitHubError(500, "/orgs", "boom")
        return self.repos

    def get_repo(self, owner, repo):
        raise AssertionError("no personal repositories in these fixtures")

    def list_runs(self, owner, repo, created_since):
        self.created_since.append(created_since)
        self.listed.append(f"{owner}/{repo}")
        return []

    def list_jobs(self, owner, repo, run_id):
        return []

    def list_org_runners(self, org):
        return []


def _collect(policy, tmp_path, client, now="2026-09-11T18:00:00Z"):
    return facts_mod.collect_facts(at(now), policy, client, tmp_path)


def _write_ledger(tmp_path, cursor):
    MonthLedger(month="2026-09", cursor=cursor).write(tmp_path)


def test_a_clean_scan_of_a_quiet_org_reports_normal_staleness(policy, tmp_path):
    """A tick whose scan succeeded is fresh by definition: the cursor is old
    because nothing ran, not because the accounting fell behind."""
    _write_ledger(tmp_path, "2026-09-11T12:00:00Z")
    facts = _collect(policy, tmp_path, FakeClient())
    assert facts.staleness == facts_mod.NORMAL
    assert facts.cursor_age_s == 60.0
    assert facts.scan_ok is True


def test_a_failed_scan_keeps_the_cursor_and_its_real_age(policy, tmp_path):
    _write_ledger(tmp_path, "2026-09-11T12:00:00Z")
    facts = _collect(policy, tmp_path, FakeClient(fail_repos=True))
    assert facts.ledger.cursor == "2026-09-11T12:00:00Z"
    assert facts.cursor_age_s == 6 * 3600
    assert facts.staleness == facts_mod.DEGRADED
    assert facts.scan_ok is False


def test_a_published_state_cursor_overrides_an_older_ledger_file(policy, tmp_path):
    """The state is written every tick; the file only on the heartbeat. Without
    this a publish-mode allocator would rescan back to the file's cursor."""
    _write_ledger(tmp_path, "2026-09-11T12:00:00Z")
    client = FakeClient(
        state={"cursor": "2026-09-11T17:30:00Z", "dry_run": False},
        repos=[{"full_name": "pbkimdev/mantra", "private": True}],
    )
    _collect(policy, tmp_path, client)
    # 17:30 minus the 60-minute overlap, not 12:00 minus it.
    assert client.created_since == ["2026-09-11T16:30:00Z"]


def test_a_dry_run_state_cursor_is_not_authority(policy, tmp_path):
    _write_ledger(tmp_path, "2026-09-11T12:00:00Z")
    client = FakeClient(
        state={"cursor": "2026-09-11T17:30:00Z", "dry_run": True},
        repos=[{"full_name": "pbkimdev/mantra", "private": True}],
    )
    _collect(policy, tmp_path, client)
    assert client.created_since == ["2026-09-11T11:00:00Z"]


def test_the_state_cursor_never_rewinds_the_ledger(policy, tmp_path):
    _write_ledger(tmp_path, "2026-09-11T17:00:00Z")
    client = FakeClient(
        state={"cursor": "2026-09-11T09:00:00Z", "dry_run": False},
        repos=[{"full_name": "pbkimdev/mantra", "private": True}],
    )
    _collect(policy, tmp_path, client)
    assert client.created_since == ["2026-09-11T16:00:00Z"]


def test_an_excluded_repository_is_never_listed(policy, tmp_path):
    """The scheduler's own jobs are public (native 0) and never routed, so they
    move no budget and no share. Scanning them would only make every tick see
    the previous tick's allocate job and force a ledger commit."""
    assert "pbkimdev/runtime-scheduler" in policy.ledger.exclude_repos
    _write_ledger(tmp_path, "2026-09-11T17:50:00Z")
    client = FakeClient(
        repos=[
            {"full_name": "pbkimdev/mantra", "private": True},
            {"full_name": "pbkimdev/runtime-scheduler", "private": False},
        ]
    )
    facts = _collect(policy, tmp_path, client)
    assert client.listed == ["pbkimdev/mantra"]
    assert facts.repos_scanned == 1
