"""One tick's fact collection: the ledger increment, runner inventory, the
previous decision's read outcomes, and the staleness verdict."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import eligibility
from .github import GitHubClient, GitHubError
from .ledger import MonthLedger, build_record, format_ts, parse_ts
from .pacing import month_bounds, month_key
from .policy import PROVIDERS, Policy

STATE_VARIABLE = "RUNTIME_STATE"
ROUTE_VARIABLES = {
    "linux": (
        "RUNTIME_ROUTE_LINUX_1",
        "RUNTIME_ROUTE_LINUX_2",
        "RUNTIME_ROUTE_LINUX_3",
    ),
    "windows": ("RUNTIME_ROUTE_WINDOWS_1", "RUNTIME_ROUTE_WINDOWS_2"),
    "macos": ("RUNTIME_ROUTE_MACOS_1", "RUNTIME_ROUTE_MACOS_2"),
}

NORMAL = "normal"
DEGRADED = "degraded"
STALE = "stale"


@dataclass
class Facts:
    now: datetime
    month: str
    month_start: datetime
    ledger: MonthLedger
    provider_states: dict[str, eligibility.ProviderState]
    staleness: str
    cursor_age_s: float
    reads: list[dict[str, Any]]
    pressure: dict[str, float | None]
    previous_state: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    repos_scanned: int = 0
    runs_scanned: int = 0
    jobs_added: int = 0
    read_failures: dict[str, int] = field(default_factory=dict)
    scan_ok: bool = True


def staleness_for(cursor_age_s: float, policy: Policy) -> str:
    two_epochs = 2 * policy.epoch_minutes * 60
    if cursor_age_s < two_epochs:
        return NORMAL
    if cursor_age_s <= 24 * 3600:
        return DEGRADED
    return STALE


def read_state(client: GitHubClient, org: str) -> dict[str, Any]:
    try:
        raw = client.get_org_variable(org, STATE_VARIABLE)
    except GitHubError:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _trailing_failures(reads: list[dict[str, Any]], provider: str) -> int:
    count = 0
    for entry in reversed(reads):
        if entry.get(provider) is False:
            count += 1
        else:
            break
    return count


def collect_facts(
    now: datetime,
    policy: Policy,
    client: GitHubClient,
    ledger_dir: Path | str,
) -> Facts:
    now = now.astimezone(UTC)
    month_start, _ = month_bounds(now)
    month = month_key(now)
    ledger = MonthLedger.load(ledger_dir, month, month_start)
    warnings: list[str] = []

    cursor = parse_ts(ledger.cursor) or month_start
    scan_from = max(
        month_start, cursor - timedelta(minutes=policy.ledger.overlap_minutes)
    )

    previous_state = read_state(client, policy.repos.org)

    repos: list[tuple[str, bool]] = []
    repo_read_ok = True
    try:
        for repo in client.list_org_repos(policy.repos.org):
            repos.append((repo["full_name"], bool(repo.get("private"))))
    except GitHubError as exc:
        repo_read_ok = False
        warnings.append(f"org repository enumeration failed: {exc}")

    for full_name in policy.repos.personal:
        owner, _, name = full_name.partition("/")
        try:
            meta = client.get_repo(owner, name)
            repos.append((meta["full_name"], bool(meta.get("private"))))
        except GitHubError as exc:
            repo_read_ok = False
            warnings.append(f"personal repository {full_name} unreadable: {exc}")

    jobs_added = 0
    runs_scanned = 0
    oldest_inflight: datetime | None = None
    for full_name, private in repos:
        owner, _, name = full_name.partition("/")
        try:
            runs = client.list_runs(owner, name, format_ts(scan_from))
        except GitHubError as exc:
            repo_read_ok = False
            warnings.append(f"run listing failed for {full_name}: {exc}")
            continue
        for run in runs:
            runs_scanned += 1
            if run.get("status") != "completed":
                created = parse_ts(run.get("created_at"))
                if created is not None and (
                    oldest_inflight is None or created < oldest_inflight
                ):
                    oldest_inflight = created
            try:
                jobs = client.list_jobs(owner, name, int(run["id"]))
            except GitHubError as exc:
                repo_read_ok = False
                warnings.append(
                    f"job listing failed for {full_name} run {run['id']}: {exc}"
                )
                continue
            for job in jobs:
                record = build_record(
                    job=job,
                    run=run,
                    repo_full_name=full_name,
                    private=private,
                    policy=policy,
                    warnings=warnings,
                )
                if (
                    record is not None
                    and record.completed >= month_start
                    and ledger.add(record)
                ):
                    jobs_added += 1

    ledger.advance_cursor(
        policy.ledger.safety_seconds,
        scanned_through=now if repo_read_ok else None,
        oldest_inflight=oldest_inflight,
    )

    runners_ok = True
    idle_archbox = 0
    archbox_label = policy.families["linux"].labels["archbox"]
    try:
        for runner in client.list_org_runners(policy.repos.org):
            labels = {entry.get("name") for entry in runner.get("labels", [])}
            if (
                runner.get("status") == "online"
                and not runner.get("busy")
                and archbox_label in labels
            ):
                idle_archbox += 1
    except GitHubError as exc:
        runners_ok = False
        warnings.append(f"runner inventory unreadable: {exc}")

    cursor_now = parse_ts(ledger.cursor) or month_start
    cursor_age_s = max(0.0, (now - cursor_now).total_seconds())
    staleness = staleness_for(cursor_age_s, policy)

    reads = list(previous_state.get("reads") or [])[-1:]
    reads.append(
        {
            "tick": format_ts(now),
            "github": repo_read_ok,
            # Blacksmith has no per-tick read; its usage comes from the daily
            # `bs usage` reconciliation, so a tick never marks it failed.
            "blacksmith": True,
            "archbox": runners_ok,
        }
    )
    read_failures = {
        provider: _trailing_failures(reads, provider) for provider in PROVIDERS
    }

    pressure: dict[str, float | None] = {}
    provider_states: dict[str, eligibility.ProviderState] = {}
    for provider in PROVIDERS:
        latency = ledger.median_latency_s(
            provider, now, policy.margins.peak_window_minutes
        )
        pressure[provider] = latency
        budget = eligibility.budget_for(
            provider,
            used=ledger.native_used(provider, owner=policy.repos.org),
            peak30=ledger.peak30(
                provider,
                now,
                policy.margins.peak_window_minutes,
                policy.margins.peak_lookback_days,
            ),
            now=now,
            policy=policy,
            stale=staleness in (DEGRADED, STALE),
        )
        provider_states[provider] = eligibility.ProviderState(
            provider=provider,
            live=policy.is_live(provider),
            hosted=policy.providers[provider].hosted,
            circuit=eligibility.circuit_state(
                now, ledger.infra_times(provider), policy
            ),
            read_failures=read_failures[provider],
            idle_runners=idle_archbox if provider == "archbox" else None,
            median_latency_s=latency,
            budget=budget,
        )

    return Facts(
        now=now,
        month=month,
        month_start=month_start,
        ledger=ledger,
        provider_states=provider_states,
        staleness=staleness,
        cursor_age_s=cursor_age_s,
        reads=reads,
        pressure=pressure,
        previous_state=previous_state,
        warnings=warnings,
        repos_scanned=len(repos),
        runs_scanned=runs_scanned,
        jobs_added=jobs_added,
        read_failures=read_failures,
        scan_ok=repo_read_ok and runners_ok,
    )
