from __future__ import annotations

import pytest

from scheduler import ledger as led
from tests.conftest import at, ledger_with, load_fixture, make_record


def run(**overrides) -> dict:
    base = {
        "id": 100,
        "event": "push",
        "status": "completed",
        "repository": {"full_name": "pbkimdev/mantra"},
        "head_repository": {"full_name": "pbkimdev/mantra"},
    }
    base.update(overrides)
    return base


def job(**overrides) -> dict:
    base = {
        "id": 1,
        "run_attempt": 1,
        "name": "linux / primary",
        "labels": ["ubuntu-latest"],
        "created_at": "2026-09-11T17:00:00Z",
        "started_at": "2026-09-11T17:00:10Z",
        "completed_at": "2026-09-11T17:01:11Z",
        "conclusion": "success",
        "steps": [{"name": "work", "conclusion": "success"}],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("labels", "provider", "family"),
    [
        (["ubuntu-latest"], "github", "linux"),
        (["windows-latest"], "github", "windows"),
        (["macos-latest"], "github", "macos"),
        (["blacksmith-4vcpu-ubuntu-2404"], "blacksmith", "linux"),
        (["blacksmith-4vcpu-windows-2025"], "blacksmith", "windows"),
        (["blacksmith-6vcpu-macos-latest"], "blacksmith", "macos"),
        (["archbox-linux-x64"], "archbox", "linux"),
        (["some-label"], "github", "linux"),
    ],
)
def test_attribution_by_label(labels, provider, family):
    assert led.provider_for(labels) == provider
    assert led.family_for(labels) == family


def test_github_private_rounds_partial_minutes_up(policy):
    # 61 seconds is two whole GitHub minutes (evidence #7).
    record = led.build_record(
        job=job(),
        run=run(),
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
    )
    assert record.native == 2.0
    assert record.normalized == 4.0


def test_public_repository_costs_no_native_units(policy):
    record = led.build_record(
        job=job(),
        run=run(),
        repo_full_name="pbkimdev/tinytalk",
        private=False,
        policy=policy,
    )
    assert record.native == 0.0
    assert record.normalized == 4.0
    assert record.routed is False


def test_fork_pull_request_is_not_routed(policy):
    forked = run(
        event="pull_request",
        head_repository={"full_name": "paulbkim-dev/mantra"},
    )
    record = led.build_record(
        job=job(),
        run=forked,
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
    )
    assert record.routed is False


def test_only_our_job_names_are_routed(policy):
    record = led.build_record(
        job=job(name="verify"),
        run=run(),
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
    )
    assert record.routed is False


def test_windows_multiplier_uses_the_github_os_table(policy):
    record = led.build_record(
        job=job(labels=["windows-latest"]),
        run=run(),
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
    )
    assert record.native == 4.0  # 2 rounded minutes x multiplier 2
    assert record.normalized == 8.0  # 2 rounded minutes x rate 4


def test_unknown_label_warns_and_rates_one(policy):
    warnings: list[str] = []
    record = led.build_record(
        job=job(labels=["mystery-runner"]),
        run=run(),
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
        warnings=warnings,
    )
    assert record.normalized == 2.0
    assert any("mystery-runner" in line for line in warnings)


@pytest.mark.parametrize(
    ("conclusion", "steps", "routed", "expected_work", "expected_infra"),
    [
        (
            "success",
            [{"name": "work", "conclusion": "success"}],
            True,
            "success",
            False,
        ),
        (
            "failure",
            [{"name": "work", "conclusion": "failure"}],
            True,
            "failure",
            False,
        ),
        # Checkout failed before the work step: the report step still ran under
        # always() and recorded `skipped` (E4c). That is infrastructure.
        ("failure", [{"name": "work", "conclusion": "skipped"}], True, "skipped", True),
        # The runner died before the report step ran at all.
        ("failure", [{"name": "work", "conclusion": None}], True, "none", True),
        # A hand-written job with no work step is a workload failure by default.
        ("failure", [{"name": "build", "conclusion": "failure"}], False, "none", False),
        (
            "cancelled",
            [{"name": "work", "conclusion": "cancelled"}],
            True,
            "cancelled",
            False,
        ),
    ],
)
def test_infrastructure_versus_workload(
    policy, conclusion, steps, routed, expected_work, expected_infra
):
    name = "linux / primary" if routed else "verify"
    record = led.build_record(
        job=job(conclusion=conclusion, steps=steps, name=name),
        run=run(),
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
    )
    assert record.work == expected_work
    assert record.infra is expected_infra


def test_running_jobs_are_not_recorded(policy):
    assert (
        led.build_record(
            job=job(completed_at=None),
            run=run(),
            repo_full_name="pbkimdev/mantra",
            private=True,
            policy=policy,
        )
        is None
    )


def test_a_rerun_attempt_is_a_new_allocation(policy):
    ledger = led.MonthLedger(month="2026-09", cursor="2026-09-01T00:00:00Z")
    first = led.build_record(
        job=job(run_attempt=1),
        run=run(),
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
    )
    second = led.build_record(
        job=job(run_attempt=2),
        run=run(),
        repo_full_name="pbkimdev/mantra",
        private=True,
        policy=policy,
    )
    assert ledger.add(first) is True
    assert ledger.add(second) is True
    assert ledger.add(first) is False  # the overlap window sees it twice
    assert len(ledger.jobs) == 2
    assert ledger.native_used("github", owner="pbkimdev") == 4.0


def test_cursor_advances_to_the_newest_completion_minus_safety():
    ledger = ledger_with(
        [
            make_record(key="a", completed_at="2026-09-11T17:05:00Z"),
            make_record(key="b", completed_at="2026-09-11T17:40:00Z"),
        ],
        cursor=at("2026-09-11T16:00:00Z"),
    )
    ledger.advance_cursor(60)
    assert ledger.cursor == "2026-09-11T17:39:00Z"


def test_cursor_never_rewinds():
    ledger = ledger_with(
        [make_record(key="a", completed_at="2026-09-11T17:05:00Z")],
        cursor=at("2026-09-11T18:00:00Z"),
    )
    ledger.advance_cursor(60)
    assert ledger.cursor == "2026-09-11T18:00:00Z"


def test_a_clean_scan_of_a_quiet_org_still_advances_the_cursor():
    ledger = led.MonthLedger(month="2026-09", cursor="2026-09-11T12:00:00Z")
    ledger.advance_cursor(60, scanned_through=at("2026-09-11T18:00:00Z"))
    assert ledger.cursor == "2026-09-11T17:59:00Z"


def test_an_in_flight_run_holds_the_cursor_behind_it():
    ledger = led.MonthLedger(month="2026-09", cursor="2026-09-11T12:00:00Z")
    ledger.advance_cursor(
        60,
        scanned_through=at("2026-09-11T18:00:00Z"),
        oldest_inflight=at("2026-09-11T17:30:00Z"),
    )
    assert ledger.cursor == "2026-09-11T17:29:00Z"


def test_peak30_is_the_worst_thirty_minute_window():
    ledger = ledger_with(
        [
            make_record(key="a", completed_at="2026-09-11T10:00:00Z", native=40),
            make_record(key="b", completed_at="2026-09-11T10:20:00Z", native=44),
            # Outside the window that starts at "a", so it never joins that sum.
            make_record(key="c", completed_at="2026-09-11T10:31:00Z", native=30),
            # Older than the lookback.
            make_record(key="d", completed_at="2026-07-01T10:00:00Z", native=900),
        ],
        cursor=at("2026-09-11T18:00:00Z"),
    )
    assert ledger.peak30("github", at("2026-09-11T18:00:00Z"), 30, 30) == 84.0


def test_median_start_latency_over_the_rolling_window():
    ledger = ledger_with(
        [
            make_record(key="a", started_at="2026-09-11T17:40:00Z", latency_s=10),
            make_record(key="b", started_at="2026-09-11T17:45:00Z", latency_s=300),
            make_record(key="c", started_at="2026-09-11T17:50:00Z", latency_s=50),
            # Outside the 30-minute window.
            make_record(key="d", started_at="2026-09-11T10:00:00Z", latency_s=9999),
        ],
        cursor=at("2026-09-11T18:00:00Z"),
    )
    assert ledger.median_latency_s("github", at("2026-09-11T18:00:00Z"), 30) == 50
    assert ledger.median_latency_s("archbox", at("2026-09-11T18:00:00Z"), 30) is None


def test_only_routed_jobs_land_in_a_family_denominator():
    ledger = ledger_with(
        [
            make_record(key="a", provider="github", normalized=100, routed=True),
            make_record(key="b", provider="github", normalized=900, routed=False),
            make_record(
                key="c",
                provider="blacksmith",
                normalized=200,
                routed=True,
                labels=("blacksmith-4vcpu-ubuntu-2404",),
            ),
        ],
        cursor=at("2026-09-11T18:00:00Z"),
    )
    shares = ledger.normalized_by_provider("linux", ("github", "blacksmith", "archbox"))
    assert shares == {"github": 100.0, "blacksmith": 200.0, "archbox": 0.0}


def test_org_and_personal_pools_stay_separate():
    ledger = ledger_with(
        [
            make_record(key="a", repo="pbkimdev/mantra", native=10),
            make_record(key="b", repo="paulbkim-dev/pkpi", native=999),
        ],
        cursor=at("2026-09-11T18:00:00Z"),
    )
    assert ledger.native_used("github", owner="pbkimdev") == 10.0


def test_round_trips_through_the_file(tmp_path):
    ledger = ledger_with(
        [make_record(key="a"), make_record(key="b", provider="archbox")],
        cursor=at("2026-09-11T18:00:00Z"),
    )
    ledger.reconciled_on = "2026-09-11"
    path = ledger.write(tmp_path)
    # One job per line keeps the daily commit diff to the jobs that arrived.
    assert path.read_text().count("\n") >= 4
    reloaded = led.MonthLedger.load(tmp_path, "2026-09", at("2026-09-01T00:00:00Z"))
    assert reloaded.cursor == ledger.cursor
    assert reloaded.reconciled_on == "2026-09-11"
    assert set(reloaded.jobs) == {"a", "b"}


def test_a_real_jobs_payload_parses(policy):
    payload = load_fixture("mantra-jobs.json")
    records = [
        led.build_record(
            job=entry,
            run=run(id=33306238235, repository={"full_name": "pbkimdev/mantra"}),
            repo_full_name="pbkimdev/mantra",
            private=True,
            policy=policy,
        )
        for entry in payload["jobs"]
    ]
    assert [r.provider for r in records] == ["blacksmith", "blacksmith"]
    assert [r.family for r in records] == ["linux", "linux"]
    # blacksmith-8vcpu-ubuntu-2404 is rate 4; the verify job ran 48 seconds,
    # which rounds to one minute.
    assert records[0].native == 4.0
    assert records[0].routed is False  # mantra does not call the scheduler yet
    assert records[0].latency_s == 7.0
