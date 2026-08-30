"""Daily reconciliation of the ledger against the vendor reports.

Reconciliation corrects the ledger. It does not replace it. Results are stored
in the ledger file, not in RUNTIME_STATE; the state carries only the alert
flags a human needs to see in the Actions tab.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .github import GitHubClient, GitHubError
from .ledger import MonthLedger, format_ts

SKU_FAMILY = {
    "Actions Linux": "linux",
    "Actions Windows": "windows",
    "Actions macOS": "macos",
}

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"


@dataclass
class SourceResult:
    source: str
    status: str
    reason: str = ""
    ledger_minutes: dict[str, float] = field(default_factory=dict)
    vendor_minutes: dict[str, float] = field(default_factory=dict)
    ledger_total: float = 0.0
    vendor_total: float = 0.0
    drift: float = 0.0
    alert: bool = False
    unattributed: dict[str, float] = field(default_factory=dict)


def drift_fraction(ledger_total: float, vendor_total: float) -> float:
    return (ledger_total - vendor_total) / max(vendor_total, 1.0)


def vendor_minutes_by_family(
    usage: Mapping[str, Any],
    private_repos: set[str] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Actions minutes from the enhanced billing usage report, by OS.

    The report lists every repository, public ones included, and a public
    repository's minutes are free. The ledger counts only what draws the
    allowance, so without the `private_repos` filter the two sides are not
    comparable and every day looks like drift.

    Returns (billable minutes by family, minutes the caller could not
    attribute). A repository that no longer exists cannot be classified, so its
    minutes land in the second dict and are recorded rather than guessed at.
    """
    totals: dict[str, float] = {}
    unattributed: dict[str, float] = {}
    for item in usage.get("usageItems", []):
        if item.get("product", "").lower() != "actions":
            continue
        if item.get("unitType") != "Minutes":
            continue
        family = SKU_FAMILY.get(item.get("sku", ""))
        if family is None:
            continue
        quantity = float(item.get("quantity", 0.0))
        repo = item.get("repositoryName", "")
        if private_repos is not None and repo not in private_repos:
            unattributed[repo] = unattributed.get(repo, 0.0) + quantity
            continue
        totals[family] = totals.get(family, 0.0) + quantity
    return totals, unattributed


def _compare(
    source: str,
    ledger_minutes: Mapping[str, float],
    vendor_minutes: Mapping[str, float],
    threshold: float,
    unattributed: Mapping[str, float] | None = None,
) -> SourceResult:
    ledger_total = sum(ledger_minutes.values())
    vendor_total = sum(vendor_minutes.values())
    drift = drift_fraction(ledger_total, vendor_total)
    return SourceResult(
        source=source,
        status=STATUS_OK,
        ledger_minutes=dict(ledger_minutes),
        vendor_minutes=dict(vendor_minutes),
        ledger_total=ledger_total,
        vendor_total=vendor_total,
        drift=round(drift, 5),
        alert=abs(drift) > threshold,
        unattributed=dict(unattributed or {}),
    )


def reconcile_github(
    client: GitHubClient,
    ledger: MonthLedger,
    now: datetime,
    owner: str,
    *,
    personal: bool,
    threshold: float,
    private_repos: set[str] | None = None,
) -> SourceResult:
    source = "github_personal" if personal else "github_org"
    try:
        usage = (
            client.user_billing_usage(owner, now.year, now.month)
            if personal
            else client.org_billing_usage(owner, now.year, now.month)
        )
    except GitHubError as exc:
        return SourceResult(
            source=source, status=STATUS_SKIPPED, reason=f"unreachable: {exc}"
        )
    vendor, unattributed = vendor_minutes_by_family(usage, private_repos)
    return _compare(
        source,
        ledger.raw_minutes_by_family(owner),
        vendor,
        threshold,
        unattributed,
    )


def reconcile_blacksmith(
    ledger: MonthLedger,
    *,
    token: str | None,
    threshold: float,
    owner: str | None = None,
    runner: str = "bs",
) -> SourceResult:
    if not token:
        return SourceResult(
            source="blacksmith", status=STATUS_SKIPPED, reason="no_token"
        )
    if shutil.which(runner) is None:
        return SourceResult(
            source="blacksmith", status=STATUS_SKIPPED, reason="bs_not_installed"
        )
    env = dict(os.environ, BLACKSMITH_TOKEN=token)
    try:
        completed = subprocess.run(  # noqa: S603
            [runner, "usage", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=True,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return SourceResult(
            source="blacksmith", status=STATUS_SKIPPED, reason=f"unreachable: {exc}"
        )
    vendor_total = float(
        payload.get("billable_minutes", payload.get("billing_minutes", 0.0))
    )
    ledger_minutes = ledger.raw_minutes_by_family(
        owner, provider="blacksmith", private_only=False
    )
    return _compare("blacksmith", ledger_minutes, {"all": vendor_total}, threshold)


def should_reconcile(now: datetime, ledger: MonthLedger, hour_utc: int) -> bool:
    """First tick at or after the configured hour each day. `reconciled_on`
    in the ledger makes that idempotent across re-runs of the same tick."""
    today = now.date().isoformat()
    return now.hour >= hour_utc and ledger.reconciled_on != today


def apply_results(
    ledger: MonthLedger,
    results: Sequence[SourceResult],
    now: datetime,
    *,
    github_os_multiplier: Mapping[str, float],
    org: str,
) -> bool:
    """Record the comparison and, beyond the threshold, snap the ledger.

    The vendor report counts raw minutes; the ledger counts native units. The
    correction converts with the same OS multipliers the ledger uses, so it
    inherits their unverified status (ADR 0004 follow-up).
    """
    alert = False
    for result in results:
        entry = asdict(result)
        entry["at"] = format_ts(now)
        ledger.reconciliations.append(entry)
        ledger.dirty = True
        if not result.alert:
            continue
        alert = True
        if result.source != "github_org":
            continue
        vendor_native = sum(
            minutes * github_os_multiplier.get(family, 1.0)
            for family, minutes in result.vendor_minutes.items()
        )
        ledger_native = ledger.native_used("github", owner=org)
        ledger.corrections.append(
            {
                "at": format_ts(now),
                "provider": "github",
                "owner": org,
                "delta": round(vendor_native - ledger_native, 5),
                "source": result.source,
            }
        )
    if alert:
        # "Double every margin for the next 24 hours." Persisted, because the
        # 96 ticks that follow are the ones that need the wider margin.
        ledger.drift_alert_until = format_ts(now + timedelta(hours=24))
    ledger.reconciled_on = now.date().isoformat()
    ledger.dirty = True
    return alert
