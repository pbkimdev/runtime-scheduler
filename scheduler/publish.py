"""Write the decision to organization Actions variables.

Routes go first and RUNTIME_STATE goes last, always. A partial publish then
leaves valid routes with a stale state, which the watchdog detects. The
reverse order would leave a state describing routes that were never written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .facts import ROUTE_VARIABLES, STATE_VARIABLE
from .github import GitHubError

# The per-variable ceiling is 48 KB. 40,000 bytes leaves room for a state that
# grows a field without a surprise 422 in production.
MAX_STATE_BYTES = 40_000

PUBLISH_ORDER = ("linux", "windows", "macos")


class StateTooLarge(Exception):
    pass


class PublishFailed(Exception):
    def __init__(self, variable: str, written: Sequence[str], cause: Exception) -> None:
        super().__init__(
            f"publishing {variable} failed after writing {list(written)}: {cause}"
        )
        self.variable = variable
        self.written = list(written)
        self.cause = cause


@dataclass
class PublishPlan:
    variable: str
    value: str
    changed: bool


@dataclass
class PublishResult:
    plans: list[PublishPlan] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False


def route_plans(
    routes_by_family: Mapping[str, Sequence[str]],
    current: Mapping[str, str],
) -> list[PublishPlan]:
    """Family is a typed argument, so a Linux label can never be written into
    a macOS variable (invariant I3)."""
    plans: list[PublishPlan] = []
    for family in PUBLISH_ORDER:
        names = ROUTE_VARIABLES[family]
        routes = list(routes_by_family[family])
        if len(routes) != len(names):
            raise ValueError(
                f"family {family} produced {len(routes)} routes for {len(names)} slots"
            )
        for name, value in zip(names, routes, strict=True):
            plans.append(PublishPlan(name, value, current.get(name) != value))
    return plans


def serialize_state(state: Mapping[str, Any]) -> str:
    payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
    size = len(payload.encode("utf-8"))
    if size > MAX_STATE_BYTES:
        raise StateTooLarge(f"RUNTIME_STATE is {size} bytes, over {MAX_STATE_BYTES}")
    return payload


def publish(
    client,
    org: str,
    routes_by_family: Mapping[str, Sequence[str]],
    state: Mapping[str, Any],
    *,
    dry_run: bool,
    current: Mapping[str, str] | None = None,
) -> PublishResult:
    if current is None:
        current = {} if dry_run else client.list_org_variables(org)
    payload = serialize_state(state)
    plans = route_plans(routes_by_family, current)
    plans.append(PublishPlan(STATE_VARIABLE, payload, True))

    result = PublishResult(plans=plans, dry_run=dry_run)
    for plan in plans:
        if not plan.changed:
            result.skipped.append(plan.variable)
            continue
        if dry_run:
            continue
        try:
            client.patch_org_variable(org, plan.variable, plan.value)
        except GitHubError as exc:
            raise PublishFailed(plan.variable, result.written, exc) from exc
        result.written.append(plan.variable)
    return result
