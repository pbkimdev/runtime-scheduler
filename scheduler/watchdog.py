"""The watchdog: reset routes when the allocator stops publishing.

It covers an allocator that crashes, hangs, or starts failing on a code
defect. It does not cover an expired token, which both workflows share.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .facts import ROUTE_VARIABLES, STATE_VARIABLE
from .ledger import format_ts, parse_ts
from .publish import serialize_state

RESET_STATUS = "watchdog_reset"

NO_STATE = "no_state"
NO_EXPIRY = "no_expires_at"
EXPIRED = "expired"
FRESH = "fresh"


@dataclass
class WatchdogDecision:
    reset: bool
    reason: str
    age_s: float | None = None


@dataclass
class WatchdogResult:
    decision: WatchdogDecision
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False


def decide(now: datetime, state: Mapping[str, Any] | None, policy) -> WatchdogDecision:
    if not state:
        return WatchdogDecision(True, NO_STATE)
    expires_at = parse_ts(state.get("expires_at"))
    if expires_at is None:
        return WatchdogDecision(True, NO_EXPIRY)
    age_s = (now.astimezone(UTC) - expires_at).total_seconds()
    limit = policy.watchdog_max_state_age_epochs * policy.epoch_minutes * 60
    if age_s > limit:
        return WatchdogDecision(True, EXPIRED, age_s)
    return WatchdogDecision(False, FRESH, age_s)


def default_routes(policy) -> dict[str, str]:
    routes: dict[str, str] = {}
    for family, names in ROUTE_VARIABLES.items():
        for name in names:
            routes[name] = policy.families[family].default_label
    return routes


def run(
    client,
    policy,
    now: datetime,
    *,
    dry_run: bool,
) -> tuple[int, WatchdogResult]:
    org = policy.repos.org
    current = client.list_org_variables(org)
    raw_state = current.get(STATE_VARIABLE)
    try:
        state = json.loads(raw_state) if raw_state else None
    except json.JSONDecodeError:
        state = None

    decision = decide(now, state, policy)
    result = WatchdogResult(decision=decision, dry_run=dry_run)
    if not decision.reset:
        return 0, result

    for name, default in default_routes(policy).items():
        if current.get(name) == default:
            result.skipped.append(name)
            continue
        if not dry_run:
            client.patch_org_variable(org, name, default)
        result.written.append(name)

    reset_state = dict(state or {})
    reset_state.update(
        {
            "status": RESET_STATUS,
            "tick": format_ts(now),
            "watchdog_reason": decision.reason,
            "routes_reset": result.written,
        }
    )
    if not dry_run:
        client.patch_org_variable(org, STATE_VARIABLE, serialize_state(reset_state))

    # Non-zero whenever a reset happened, not only when a route value moved:
    # the red Actions tab is the signal that the allocator stopped publishing,
    # and routes already sitting at their defaults do not make that untrue. A
    # dry run reports the same condition without writing, so a
    # disabled-but-dispatched watchdog still surfaces it.
    return 1, result
