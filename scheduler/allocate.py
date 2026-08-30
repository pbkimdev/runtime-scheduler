"""One tick of allocation: eligibility, deficit selection, and route labels."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import eligibility
from .ledger import format_ts

STATUS_OK = "ok"
STATUS_PACING = "pacing_exceeded"
STATUS_NONE = "no_eligible_runtime"

_SEVERITY = {STATUS_OK: 0, STATUS_PACING: 1, STATUS_NONE: 2}


@dataclass(frozen=True)
class FamilyDecision:
    family: str
    status: str
    providers: tuple[str, ...]
    routes: tuple[str, ...]
    deficits: dict[str, float]
    shares: dict[str, float]
    rejected: dict[str, str]


@dataclass
class Decision:
    tick: datetime
    status: str
    expires_at: datetime
    families: dict[str, FamilyDecision] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def shares_for(
    normalized: dict[str, float], capable: Sequence[str]
) -> dict[str, float]:
    total = sum(normalized.get(provider, 0.0) for provider in capable)
    if total <= 0:
        return {provider: 0.0 for provider in capable}
    return {provider: normalized.get(provider, 0.0) / total for provider in capable}


def by_deficit(
    providers: Sequence[str],
    deficits: dict[str, float],
    tiebreak: Sequence[str],
) -> list[str]:
    """Largest deficit first, then the fixed order archbox, blacksmith, github."""
    return sorted(providers, key=lambda p: (-deficits[p], tiebreak.index(p)))


def allocate(now: datetime, policy, facts) -> Decision:
    decision = Decision(
        tick=now,
        status=STATUS_OK,
        expires_at=now
        + timedelta(minutes=policy.epoch_minutes * policy.route_expiry_epochs),
        warnings=list(facts.warnings),
    )

    for name, family in policy.families.items():
        capable = family.capable
        primary: list[str] = []
        fallback: list[str] = []
        rejected: dict[str, str] = {}

        for provider in capable:
            reason = eligibility.reject_reason(
                facts.provider_states[provider], facts.staleness, policy
            )
            if reason is None:
                primary.append(provider)
                fallback.append(provider)
            elif reason.fallback_ok:
                fallback.append(provider)
                rejected[provider] = reason.code
            else:
                rejected[provider] = reason.code

        normalized = facts.ledger.normalized_by_provider(name, capable)
        shares = shares_for(normalized, capable)
        target = 1.0 / len(capable)
        deficits = {provider: target - shares[provider] for provider in capable}

        if primary:
            first = by_deficit(primary, deficits, policy.tiebreak)[0]
            chosen = [first] + [
                p for p in by_deficit(fallback, deficits, policy.tiebreak) if p != first
            ]
            status = STATUS_OK
        elif fallback:
            chosen = by_deficit(fallback, deficits, policy.tiebreak)
            status = STATUS_PACING
        else:
            chosen = []
            status = STATUS_NONE

        labels = [family.labels[provider] for provider in chosen]
        while len(labels) < family.route_slots:
            labels.append(family.default_label)
        labels = labels[: family.route_slots]

        decision.families[name] = FamilyDecision(
            family=name,
            status=status,
            providers=tuple(chosen),
            routes=tuple(labels),
            deficits={p: round(deficits[p], 5) for p in capable},
            shares={p: round(shares[p], 5) for p in capable},
            rejected=rejected,
        )
        if _SEVERITY[status] > _SEVERITY[decision.status]:
            decision.status = status

    return decision


def state_json(
    decision: Decision,
    facts,
    policy,
    *,
    dry_run: bool,
    drift_alert: bool = False,
    reconciliation_skipped: Sequence[str] = (),
) -> dict:
    """The RUNTIME_STATE payload. Reconciliation results live in the ledger,
    not here; the state carries only the alert flags."""
    return {
        "schema": 1,
        "status": decision.status,
        "tick": format_ts(decision.tick),
        "expires_at": format_ts(decision.expires_at),
        "families": {
            name: {
                "status": family.status,
                "routes": list(family.routes),
                "deficits": family.deficits,
                "shares": family.shares,
            }
            for name, family in decision.families.items()
        },
        "rejected": {
            name: family.rejected for name, family in decision.families.items()
        },
        "budgets": {
            provider: state.budget.as_state()
            for provider, state in facts.provider_states.items()
        },
        "cursor": facts.ledger.cursor,
        "staleness": facts.staleness,
        "circuits": {
            provider: state.circuit for provider, state in facts.provider_states.items()
        },
        "reads": facts.reads,
        "pressure": {
            provider: (None if value is None else round(value, 5))
            for provider, value in facts.pressure.items()
        },
        "drift_alert": drift_alert,
        "reconciliation_skipped": list(reconciliation_skipped),
        "dry_run": dry_run,
    }
