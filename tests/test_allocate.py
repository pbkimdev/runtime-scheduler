from __future__ import annotations

import pytest

from scheduler import eligibility as el
from scheduler.allocate import allocate, state_json
from scheduler.facts import Facts
from tests.conftest import at, ledger_with, make_record

NOW = at("2026-09-11T18:00:00Z")


def linux_units(github: float, blacksmith: float, archbox: float):
    """Routed Linux jobs carrying the worked example's normalized totals."""
    return [
        make_record(key="g", provider="github", normalized=github),
        make_record(
            key="b",
            provider="blacksmith",
            normalized=blacksmith,
            labels=("blacksmith-4vcpu-ubuntu-2404",),
        ),
        make_record(
            key="a",
            provider="archbox",
            normalized=archbox,
            labels=("archbox-linux-x64",),
        ),
    ]


def facts_for(
    policy,
    *,
    records,
    used: dict[str, float],
    peak30: dict[str, float],
    idle_archbox: int = 1,
    circuits: dict[str, str] | None = None,
    staleness: str = "normal",
    latency: dict[str, float] | None = None,
    live: dict[str, bool] | None = None,
) -> Facts:
    ledger = ledger_with(records, cursor=NOW)
    circuits = circuits or {}
    latency = latency or {}
    live = live or {}
    states = {}
    for provider in ("github", "blacksmith", "archbox"):
        states[provider] = el.ProviderState(
            provider=provider,
            live=live.get(provider, policy.is_live(provider)),
            hosted=policy.providers[provider].hosted,
            circuit=circuits.get(provider, el.CLOSED),
            read_failures=0,
            idle_runners=idle_archbox if provider == "archbox" else None,
            median_latency_s=latency.get(provider),
            budget=el.budget_for(
                provider,
                used=used.get(provider, 0.0),
                peak30=peak30.get(provider, 0.0),
                now=NOW,
                policy=policy,
                stale=staleness != "normal",
            ),
        )
    return Facts(
        now=NOW,
        month="2026-09",
        month_start=at("2026-09-01T00:00:00Z"),
        ledger=ledger,
        provider_states=states,
        staleness=staleness,
        cursor_age_s=0.0,
        reads=[],
        pressure={p: latency.get(p) for p in states},
        previous_state={},
    )


def worked_example(policy) -> Facts:
    return facts_for(
        policy,
        records=linux_units(1220, 1180, 1200),
        used={"github": 610, "blacksmith": 1180},
        peak30={"github": 84, "blacksmith": 150},
    )


def test_worked_example_routes(plan_policy):
    """The plan's tick at 2026-09-11T18:00Z, derived end to end.

    Blacksmith has the largest deficit but is over its paced budget, so it
    cannot be route _1. That is invariant I1: eligibility filters before the
    deficit is consulted.
    """
    decision = allocate(NOW, plan_policy, worked_example(plan_policy))
    linux = decision.families["linux"]
    assert linux.status == "ok"
    assert linux.routes == (
        "archbox-linux-x64",
        "blacksmith-4vcpu-ubuntu-2404",
        "ubuntu-latest",
    )
    assert linux.rejected == {"blacksmith": "pacing_exceeded"}
    assert linux.shares == pytest.approx(
        {"github": 0.33889, "blacksmith": 0.32778, "archbox": 0.33333}, abs=1e-5
    )
    assert linux.deficits == pytest.approx(
        {"github": -0.00556, "blacksmith": 0.00556, "archbox": 0.0}, abs=1e-5
    )


def test_worked_example_budgets_reach_the_state(plan_policy):
    facts = worked_example(plan_policy)
    decision = allocate(NOW, plan_policy, facts)
    state = state_json(decision, facts, plan_policy, dry_run=True)
    assert state["budgets"]["github"]["used"] == 610
    assert state["budgets"]["github"]["margin"] == 84
    assert state["budgets"]["github"]["cap"] == pytest.approx(915.0, abs=0.1)
    assert state["budgets"]["blacksmith"]["cap"] == pytest.approx(1220.0, abs=0.1)
    assert state["expires_at"] == "2026-09-11T18:45:00Z"
    assert state["cursor"] == "2026-09-11T18:00:00Z"


def test_a_non_live_provider_never_lands_in_a_route(policy):
    """The shipped policy has only GitHub live, so every route is the family
    default and the other two show up as rejected."""
    facts = facts_for(
        policy,
        records=linux_units(1220, 1180, 1200),
        used={"github": 610, "blacksmith": 1180},
        peak30={"github": 84, "blacksmith": 150},
    )
    decision = allocate(NOW, policy, facts)
    assert decision.families["linux"].routes == (
        "ubuntu-latest",
        "ubuntu-latest",
        "ubuntu-latest",
    )
    assert decision.families["linux"].rejected == {
        "blacksmith": "not_live",
        "archbox": "not_live",
    }
    assert decision.families["windows"].routes == ("windows-latest", "windows-latest")
    assert decision.families["macos"].routes == ("macos-latest", "macos-latest")
    assert decision.status == "ok"


def test_a_half_open_provider_is_never_route_one(plan_policy):
    facts = facts_for(
        plan_policy,
        records=linux_units(0, 0, 3000),  # archbox has by far the worst deficit
        used={},
        peak30={},
        circuits={"archbox": el.HALF_OPEN},
    )
    decision = allocate(NOW, plan_policy, facts)
    routes = decision.families["linux"].routes
    assert routes[0] != "archbox-linux-x64"
    assert "archbox-linux-x64" in routes
    assert decision.families["linux"].rejected["archbox"] == "circuit_half_open"


def test_the_tiebreak_settles_an_exact_draw(plan_policy):
    facts = facts_for(plan_policy, records=[], used={}, peak30={})
    decision = allocate(NOW, plan_policy, facts)
    # No history: every deficit is 1/3, so the fixed order decides.
    assert decision.families["linux"].routes == (
        "archbox-linux-x64",
        "blacksmith-4vcpu-ubuntu-2404",
        "ubuntu-latest",
    )


def test_slots_fill_with_the_family_default(plan_policy):
    facts = facts_for(
        plan_policy,
        records=[],
        used={},
        peak30={},
        idle_archbox=0,
        circuits={"blacksmith": el.OPEN},
    )
    linux = allocate(NOW, plan_policy, facts).families["linux"]
    assert linux.routes == ("ubuntu-latest", "ubuntu-latest", "ubuntu-latest")
    assert linux.rejected == {
        "blacksmith": "circuit_open",
        "archbox": "no_idle_capacity",
    }


def test_pacing_exceeded_status_when_nothing_is_primary_eligible(plan_policy):
    facts = facts_for(
        plan_policy,
        records=linux_units(100, 100, 100),
        used={"github": 1700, "blacksmith": 2300},
        peak30={"github": 60, "blacksmith": 60},
        idle_archbox=0,
    )
    linux = allocate(NOW, plan_policy, facts).families["linux"]
    assert linux.status == "pacing_exceeded"
    assert linux.routes[0] in (
        "ubuntu-latest",
        "blacksmith-4vcpu-ubuntu-2404",
    )


def test_no_eligible_runtime_resets_to_the_default(plan_policy):
    facts = facts_for(
        plan_policy,
        records=[],
        used={"github": 1800, "blacksmith": 2400},
        peak30={"github": 60, "blacksmith": 60},
        idle_archbox=0,
    )
    decision = allocate(NOW, plan_policy, facts)
    assert decision.status == "no_eligible_runtime"
    assert decision.families["linux"].routes == (
        "ubuntu-latest",
        "ubuntu-latest",
        "ubuntu-latest",
    )
