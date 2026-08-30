from __future__ import annotations

from datetime import timedelta

from scheduler import eligibility as el
from tests.conftest import at

NOW = at("2026-09-11T18:00:00Z")


def state(provider="github", **overrides) -> el.ProviderState:
    budget = overrides.pop(
        "budget",
        el.Budget(provider, 2000.0, 100.0, 60.0, 900.0, 1800.0),
    )
    base = dict(
        provider=provider,
        live=True,
        hosted=True,
        circuit=el.CLOSED,
        read_failures=0,
        idle_runners=None,
        median_latency_s=None,
        budget=budget,
    )
    base.update(overrides)
    return el.ProviderState(**base)


def reason(st, policy, staleness="normal"):
    found = el.reject_reason(st, staleness, policy)
    return None if found is None else found.code


def test_a_healthy_provider_has_no_reason(policy):
    assert reason(state(), policy) is None


def test_not_live_beats_everything(policy):
    # A non-live provider is rejected before anything else is even looked at.
    st = state(live=False, circuit=el.OPEN, read_failures=9)
    assert reason(st, policy) == "not_live"
    assert el.reject_reason(st, "normal", policy).fallback_ok is False


def test_circuit_open_is_not_fallback_eligible(policy):
    assert reason(state(circuit=el.OPEN), policy) == "circuit_open"
    assert (
        el.reject_reason(state(circuit=el.OPEN), "normal", policy).fallback_ok is False
    )


def test_half_open_stays_fallback_eligible(policy):
    found = el.reject_reason(state(circuit=el.HALF_OPEN), "normal", policy)
    assert found.code == "circuit_half_open"
    assert found.fallback_ok is True


def test_two_failed_read_ticks_reject_the_provider(policy):
    assert reason(state(read_failures=2), policy) == "unhealthy_reads"


def test_archbox_with_no_idle_runner(policy):
    st = state(
        "archbox",
        hosted=False,
        idle_runners=0,
        budget=el.Budget("archbox", None, 0.0, 60.0, None, None),
    )
    assert reason(st, policy) == "no_idle_capacity"


def test_queue_pressure_gates_a_hosted_provider(policy):
    assert reason(state(median_latency_s=300.0), policy) == "queue_pressure"


def test_pacing_exceeded_keeps_the_fallback(policy):
    budget = el.Budget("blacksmith", 3000.0, 1180.0, 150.0, 1220.0, 2400.0)
    found = el.reject_reason(state("blacksmith", budget=budget), "normal", policy)
    assert found.code == "pacing_exceeded"
    assert found.fallback_ok is True


def test_reserve_exceeded_does_not(policy):
    budget = el.Budget("blacksmith", 3000.0, 2400.0, 150.0, 1220.0, 2400.0)
    found = el.reject_reason(state("blacksmith", budget=budget), "normal", policy)
    assert found.code == "reserve_exceeded"
    assert found.fallback_ok is False


def test_a_hard_reason_outranks_a_soft_one(policy):
    # Half-open and over the hard reserve at once must not stay fallback-eligible.
    budget = el.Budget("blacksmith", 3000.0, 2400.0, 150.0, 1220.0, 2400.0)
    found = el.reject_reason(
        state("blacksmith", circuit=el.HALF_OPEN, budget=budget), "normal", policy
    )
    assert found.fallback_ok is False


def test_a_stale_ledger_demotes_hosted_providers_only(policy):
    found = el.reject_reason(state(), "stale", policy)
    assert found.code == "stale_ledger"
    assert found.fallback_ok is True
    archbox = state(
        "archbox",
        hosted=False,
        idle_runners=2,
        budget=el.Budget("archbox", None, 0.0, 60.0, None, None),
    )
    assert el.reject_reason(archbox, "stale", policy) is None


def test_margins_double_when_the_ledger_is_behind(policy):
    fresh = el.budget_for(
        "github", used=0, peak30=84, now=NOW, policy=policy, stale=False
    )
    stale = el.budget_for(
        "github", used=0, peak30=84, now=NOW, policy=policy, stale=True
    )
    assert fresh.margin == 84
    assert stale.margin == 168


def test_margin_floor_carries_a_cold_ledger(policy):
    budget = el.budget_for(
        "github", used=0, peak30=0, now=NOW, policy=policy, stale=False
    )
    assert budget.margin == 60


def test_archbox_has_no_budget_at_all(policy):
    budget = el.budget_for(
        "archbox", used=0, peak30=0, now=NOW, policy=policy, stale=False
    )
    assert budget.cap_hard is None and budget.cap_pace is None


def _infra(count: int, first, spacing_minutes: int = 5):
    return [first + timedelta(minutes=spacing_minutes * i) for i in range(count)]


def test_circuit_closed_below_the_threshold(policy):
    times = _infra(2, NOW - timedelta(minutes=10))
    assert el.circuit_state(NOW, times, policy) == el.CLOSED


def test_three_failures_in_the_window_open_the_circuit(policy):
    times = _infra(3, NOW - timedelta(minutes=20))
    assert el.circuit_state(NOW, times, policy) == el.OPEN


def test_failures_spread_beyond_the_window_do_not_trip(policy):
    times = _infra(3, NOW - timedelta(minutes=90), spacing_minutes=25)
    assert el.circuit_state(NOW, times, policy) == el.CLOSED


def test_the_circuit_half_opens_after_four_epochs(policy):
    trip = NOW - timedelta(minutes=65)
    times = _infra(3, trip - timedelta(minutes=10), spacing_minutes=5)
    assert el.circuit_state(NOW, times, policy) == el.HALF_OPEN


def test_a_failure_while_half_open_reopens_it(policy):
    times = _infra(3, NOW - timedelta(minutes=75), spacing_minutes=5)
    times.append(NOW - timedelta(minutes=2))
    assert el.circuit_state(NOW, times, policy) == el.OPEN


def test_the_circuit_closes_after_a_clean_half_open_epoch(policy):
    times = _infra(3, NOW - timedelta(minutes=100), spacing_minutes=5)
    assert el.circuit_state(NOW, times, policy) == el.CLOSED
