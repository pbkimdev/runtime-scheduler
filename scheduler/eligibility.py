"""Provider eligibility: circuit state, epoch budgets, and reject reasons.

Eligibility is decided before cost or load balancing (invariant I1). The
deficit computation never adds a provider to the eligible set, so every reason
recorded here is final for the tick.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import pacing

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


@dataclass(frozen=True)
class Reason:
    code: str
    fallback_ok: bool


# A provider that is not live is rejected before anything else and never lands
# in a route. `circuit_half_open` and `pacing_exceeded` keep a provider as a
# fallback: half-open wants real traffic to recover on, and a paced-out
# provider is still well inside its hard reserve.
NOT_LIVE = Reason("not_live", False)
CIRCUIT_OPEN = Reason("circuit_open", False)
CIRCUIT_HALF_OPEN = Reason("circuit_half_open", True)
UNHEALTHY_READS = Reason("unhealthy_reads", False)
NO_IDLE_CAPACITY = Reason("no_idle_capacity", False)
QUEUE_PRESSURE = Reason("queue_pressure", False)
PACING_EXCEEDED = Reason("pacing_exceeded", True)
RESERVE_EXCEEDED = Reason("reserve_exceeded", False)
STALE_LEDGER = Reason("stale_ledger", True)


@dataclass(frozen=True)
class Budget:
    provider: str
    allowance: float | None
    used: float
    margin: float
    cap_pace: float | None
    cap_hard: float | None

    def as_state(self) -> dict[str, float | None]:
        return {
            "used": round(self.used, 5),
            "margin": round(self.margin, 5),
            "cap": None if self.cap_pace is None else round(self.cap_pace, 5),
            "cap_hard": None if self.cap_hard is None else round(self.cap_hard, 5),
        }


@dataclass(frozen=True)
class ProviderState:
    provider: str
    live: bool
    hosted: bool
    circuit: str
    read_failures: int
    idle_runners: int | None
    median_latency_s: float | None
    budget: Budget


def circuit_state(
    now: datetime,
    infra_times: Sequence[datetime],
    policy,
) -> str:
    """Derived, never stored: a lost RUNTIME_STATE costs one epoch of circuit
    memory, not a stuck-open provider.

    The trip point is the newest infrastructure failure that closed a window of
    `trip_infra_failures` inside `trip_window_minutes`. The circuit is open for
    `open_epochs`, half-open for the epoch after that, and reopens if any
    infrastructure failure landed after the trip.
    """
    trip = _trip_time(infra_times, policy)
    if trip is None:
        return CLOSED

    open_duration = timedelta(minutes=policy.circuit.open_epochs * policy.epoch_minutes)
    age = now - trip
    if age < open_duration:
        return OPEN
    if age < open_duration + timedelta(minutes=policy.epoch_minutes):
        if infra_times and max(infra_times) > trip:
            return OPEN
        return HALF_OPEN
    return CLOSED


def _trip_time(infra_times: Sequence[datetime], policy) -> datetime | None:
    times = sorted(infra_times)
    threshold = policy.circuit.trip_infra_failures
    window = timedelta(minutes=policy.circuit.trip_window_minutes)
    trip: datetime | None = None
    for index, stamp in enumerate(times):
        if index + 1 < threshold:
            continue
        if stamp - times[index + 1 - threshold] <= window:
            trip = stamp
    return trip


def budget_for(
    provider: str,
    *,
    used: float,
    peak30: float,
    now: datetime,
    policy,
    stale: bool,
) -> Budget:
    allowance = policy.allowance(provider)
    margin = max(policy.margins.floor_native_units, peak30)
    if stale:
        margin *= policy.margins.stale_multiplier
    if allowance is None:
        return Budget(provider, None, used, margin, None, None)
    reserve = policy.reserves[provider]
    return Budget(
        provider=provider,
        allowance=allowance,
        used=used,
        margin=margin,
        cap_pace=pacing.cap_pace(allowance, reserve, now, policy.slack),
        cap_hard=pacing.cap_hard(allowance, reserve),
    )


def reasons_for(state: ProviderState, staleness: str, policy) -> list[Reason]:
    reasons: list[Reason] = []
    if not state.live:
        return [NOT_LIVE]

    if state.read_failures >= policy.circuit.trip_read_failures:
        reasons.append(UNHEALTHY_READS)
    if state.circuit == OPEN:
        reasons.append(CIRCUIT_OPEN)
    elif state.circuit == HALF_OPEN:
        reasons.append(CIRCUIT_HALF_OPEN)

    if state.hosted:
        latency = state.median_latency_s
        if latency is not None and latency > policy.capacity.max_start_latency_s:
            reasons.append(QUEUE_PRESSURE)
    else:
        idle = state.idle_runners or 0
        if idle < policy.capacity.archbox_min_idle_runners:
            reasons.append(NO_IDLE_CAPACITY)

    budget = state.budget
    if budget.cap_hard is not None and budget.cap_pace is not None:
        headroom = budget.used + budget.margin
        if headroom > budget.cap_hard:
            reasons.append(RESERVE_EXCEEDED)
        elif headroom > budget.cap_pace:
            reasons.append(PACING_EXCEEDED)

    # Over 24 hours of ledger lag, hosted providers become fallback-only. The
    # degradation direction is always toward the provider that cannot cost
    # money (invariant I7).
    if staleness == "stale" and state.hosted:
        reasons.append(STALE_LEDGER)

    return reasons


def reject_reason(state: ProviderState, staleness: str, policy) -> Reason | None:
    """The most severe applicable reason, so a half-open provider that is also
    over its hard reserve is not quietly kept as a fallback."""
    reasons = reasons_for(state, staleness, policy)
    if not reasons:
        return None
    for reason in reasons:
        if not reason.fallback_ok:
            return reason
    return reasons[0]
