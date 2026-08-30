from __future__ import annotations

import pytest

from scheduler import reconcile as rec
from scheduler.github import GitHubError
from scheduler.ledger import MonthLedger
from tests.conftest import at, ledger_with, make_record

NOW = at("2026-09-11T03:04:00Z")

USAGE = {
    "usageItems": [
        {
            "product": "actions",
            "sku": "Actions Linux",
            "unitType": "Minutes",
            "quantity": 100,
        },
        {
            "product": "actions",
            "sku": "Actions Linux",
            "unitType": "Minutes",
            "quantity": 20,
        },
        {
            "product": "actions",
            "sku": "Actions Windows",
            "unitType": "Minutes",
            "quantity": 30,
        },
        # Not Actions minutes: storage rows and shared-storage GB never count.
        {
            "product": "actions",
            "sku": "Actions Linux",
            "unitType": "GigabyteHours",
            "quantity": 900,
        },
        {
            "product": "packages",
            "sku": "Packages",
            "unitType": "Minutes",
            "quantity": 900,
        },
    ]
}


class FakeClient:
    def __init__(self, usage=None, error=None):
        self.usage = usage
        self.error = error

    def org_billing_usage(self, org, year, month):
        if self.error:
            raise self.error
        return self.usage

    def user_billing_usage(self, user, year, month):
        if self.error:
            raise self.error
        return self.usage


def test_only_actions_minutes_reach_the_comparison():
    assert rec.vendor_minutes_by_family(USAGE) == {"linux": 120.0, "windows": 30.0}


@pytest.mark.parametrize(
    ("ledger_total", "vendor_total", "expected"),
    [
        (150, 150, 0.0),
        (157, 150, pytest.approx(0.04667, abs=1e-5)),
        (200, 150, pytest.approx(0.33333, abs=1e-5)),
        (0, 150, -1.0),
        # A vendor total of zero must not divide by zero.
        (3, 0, 3.0),
    ],
)
def test_drift_arithmetic(ledger_total, vendor_total, expected):
    assert rec.drift_fraction(ledger_total, vendor_total) == expected


def _ledger():
    # 120 rounded Linux minutes and 30 Windows minutes on the org, matching the
    # vendor report exactly.
    return ledger_with(
        [
            make_record(
                key="a",
                started_at="2026-09-01T00:00:00Z",
                completed_at="2026-09-01T02:00:00Z",
                native=120.0,
            ),
            make_record(
                key="b",
                family="windows",
                labels=("windows-latest",),
                started_at="2026-09-01T03:00:00Z",
                completed_at="2026-09-01T03:30:00Z",
                native=60.0,
            ),
        ],
        cursor=NOW,
    )


def test_a_matching_ledger_raises_no_alert():
    result = rec.reconcile_github(
        FakeClient(USAGE), _ledger(), NOW, "pbkimdev", personal=False, threshold=0.05
    )
    assert result.status == rec.STATUS_OK
    assert result.ledger_total == 150.0
    assert result.vendor_total == 150.0
    assert result.alert is False


def test_drift_beyond_the_threshold_alerts_and_snaps_the_ledger():
    ledger = _ledger()
    thin = {
        "usageItems": [
            {
                "product": "actions",
                "sku": "Actions Linux",
                "unitType": "Minutes",
                "quantity": 60,
            }
        ]
    }
    result = rec.reconcile_github(
        FakeClient(thin), ledger, NOW, "pbkimdev", personal=False, threshold=0.05
    )
    assert result.alert is True
    alert = rec.apply_results(
        ledger,
        [result],
        NOW,
        github_os_multiplier={"linux": 1, "windows": 2, "macos": 10},
        org="pbkimdev",
    )
    assert alert is True
    # The ledger held 120 linux + 60 windows native units; the vendor says 60.
    assert ledger.corrections[0]["delta"] == pytest.approx(-120.0)
    assert ledger.native_used("github", owner="pbkimdev") == pytest.approx(60.0)
    assert ledger.reconciled_on == "2026-09-11"


def test_an_unreachable_vendor_is_skipped_not_guessed():
    result = rec.reconcile_github(
        FakeClient(error=GitHubError(403, "u", "no billing scope")),
        _ledger(),
        NOW,
        "pbkimdev",
        personal=False,
        threshold=0.05,
    )
    assert result.status == rec.STATUS_SKIPPED
    assert "unreachable" in result.reason


def test_blacksmith_without_a_token_is_skipped():
    result = rec.reconcile_blacksmith(_ledger(), token=None, threshold=0.05)
    assert result.status == rec.STATUS_SKIPPED
    assert result.reason == "no_token"


def test_blacksmith_without_the_cli_is_skipped():
    result = rec.reconcile_blacksmith(
        _ledger(), token="x", threshold=0.05, runner="bs-not-installed-anywhere"
    )
    assert result.status == rec.STATUS_SKIPPED
    assert result.reason == "bs_not_installed"


def test_reconciliation_runs_once_a_day():
    ledger = MonthLedger(month="2026-09", cursor="2026-09-11T03:00:00Z")
    assert rec.should_reconcile(at("2026-09-11T02:45:00Z"), ledger, 3) is False
    assert rec.should_reconcile(at("2026-09-11T03:00:00Z"), ledger, 3) is True
    ledger.reconciled_on = "2026-09-11"
    assert rec.should_reconcile(at("2026-09-11T03:15:00Z"), ledger, 3) is False
    assert rec.should_reconcile(at("2026-09-12T03:00:00Z"), ledger, 3) is True
