"""Load and validate policy.toml into a frozen tree.

A parse or validation error raises PolicyError. The CLI turns that into exit
code 2 with nothing written, so a bad policy never publishes a route.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

FAMILIES = ("linux", "windows", "macos")
PROVIDERS = ("github", "blacksmith", "archbox")


class PolicyError(Exception):
    """policy.toml is missing a key, or carries a value the allocator rejects."""


@dataclass(frozen=True)
class Provider:
    name: str
    hosted: bool
    live: bool


@dataclass(frozen=True)
class Family:
    name: str
    capable: tuple[str, ...]
    route_slots: int
    default_label: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class Margins:
    floor_native_units: float
    peak_window_minutes: int
    peak_lookback_days: int
    stale_multiplier: float


@dataclass(frozen=True)
class Capacity:
    archbox_min_idle_runners: int
    max_start_latency_s: float


@dataclass(frozen=True)
class Circuit:
    trip_infra_failures: int
    trip_window_minutes: int
    trip_read_failures: int
    open_epochs: int


@dataclass(frozen=True)
class Repos:
    org: str
    scan: str
    personal_owner: str
    personal: tuple[str, ...]


@dataclass(frozen=True)
class LedgerPolicy:
    overlap_minutes: int
    safety_seconds: int


@dataclass(frozen=True)
class Reconciliation:
    hour_utc: int
    drift_alert_fraction: float


@dataclass(frozen=True)
class Policy:
    epoch_minutes: int
    route_expiry_epochs: int
    band_percentage_points: float
    tiebreak: tuple[str, ...]
    slack: float
    reserves: Mapping[str, float]
    margins: Margins
    capacity: Capacity
    circuit: Circuit
    allowances: Mapping[str, float]
    providers: Mapping[str, Provider]
    families: Mapping[str, Family]
    repos: Repos
    ledger: LedgerPolicy
    watchdog_max_state_age_epochs: int
    native_rates: tuple[tuple[str, float], ...]
    normalized_rates: tuple[tuple[str, float], ...]
    github_os_multiplier: Mapping[str, float]
    reconciliation: Reconciliation
    rescue_enabled: bool
    rescue_max_queue_minutes: int

    def allowance(self, provider: str) -> float | None:
        """Native allowance that routing may spend, or None for no allowance.

        GitHub's routed work lives in organization repositories, so the org
        pool is the one routing can overspend. The personal pool is tracked for
        reconciliation only (E7: the two pools are separate).
        """
        if provider == "github":
            return self.allowances["github_org_native_minutes"]
        if provider == "blacksmith":
            return self.allowances["blacksmith_native_minutes"]
        return None

    def is_live(self, provider: str) -> bool:
        return self.providers[provider].live


def _req(table: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in table:
        raise PolicyError(f"{where}: missing key {key!r}")
    return table[key]


def _fraction(value: Any, where: str) -> float:
    if not isinstance(value, (int, float)) or not 0.0 <= float(value) < 1.0:
        raise PolicyError(f"{where}: expected a fraction in [0, 1), got {value!r}")
    return float(value)


def _rate_table(table: Mapping[str, Any], where: str) -> tuple[tuple[str, float], ...]:
    rows: list[tuple[str, float]] = []
    for pattern, rate in table.items():
        if not isinstance(rate, (int, float)) or rate < 0:
            raise PolicyError(f"{where}: rate for {pattern!r} must be >= 0")
        rows.append((pattern, float(rate)))
    if not rows:
        raise PolicyError(f"{where}: empty rate table")
    return tuple(rows)


def load_policy(path: str | Path) -> Policy:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"policy file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{path}: {exc}") from exc
    return parse_policy(raw, str(path))


def parse_policy(raw: Mapping[str, Any], where: str = "policy") -> Policy:
    epoch = _req(raw, "epoch", where)
    allocation = _req(raw, "allocation", where)
    pacing = _req(raw, "pacing", where)
    reserves_raw = _req(raw, "reserves", where)
    margins_raw = _req(raw, "margins", where)
    capacity_raw = _req(raw, "capacity", where)
    circuit_raw = _req(raw, "circuit", where)
    allowances_raw = _req(raw, "allowances", where)
    providers_raw = _req(raw, "providers", where)
    families_raw = _req(raw, "families", where)
    repos_raw = _req(raw, "repos", where)
    ledger_raw = _req(raw, "ledger", where)
    watchdog_raw = _req(raw, "watchdog", where)
    norm_raw = _req(raw, "normalization", where)
    recon_raw = _req(raw, "reconciliation", where)
    rescue_raw = _req(raw, "rescue", where)

    epoch_minutes = int(_req(epoch, "minutes", "[epoch]"))
    if epoch_minutes <= 0:
        raise PolicyError("[epoch]: minutes must be positive")

    tiebreak = tuple(_req(allocation, "tiebreak", "[allocation]"))
    if set(tiebreak) != set(PROVIDERS):
        raise PolicyError(
            f"[allocation]: tiebreak must name every provider {PROVIDERS}, "
            f"got {tiebreak}"
        )

    providers: dict[str, Provider] = {}
    for name in PROVIDERS:
        entry = _req(providers_raw, name, "[providers]")
        providers[name] = Provider(
            name=name,
            hosted=bool(_req(entry, "hosted", f"[providers.{name}]")),
            live=bool(_req(entry, "live", f"[providers.{name}]")),
        )

    reserves: dict[str, float] = {}
    for name in ("github", "blacksmith"):
        reserves[name] = _fraction(_req(reserves_raw, name, "[reserves]"), "[reserves]")
    reserves["archbox"] = 0.0

    families: dict[str, Family] = {}
    for name in FAMILIES:
        entry = _req(families_raw, name, "[families]")
        capable = tuple(_req(entry, "capable", f"[families.{name}]"))
        unknown = set(capable) - set(PROVIDERS)
        if unknown:
            raise PolicyError(f"[families.{name}]: unknown providers {sorted(unknown)}")
        labels = dict(_req(entry, "labels", f"[families.{name}]"))
        missing = set(capable) - set(labels)
        if missing:
            raise PolicyError(
                f"[families.{name}.labels]: no label for {sorted(missing)}"
            )
        slots = int(_req(entry, "route_slots", f"[families.{name}]"))
        if slots < 1:
            raise PolicyError(f"[families.{name}]: route_slots must be >= 1")
        families[name] = Family(
            name=name,
            capable=capable,
            route_slots=slots,
            default_label=str(_req(entry, "default_label", f"[families.{name}]")),
            labels=MappingProxyType(labels),
        )

    scan = str(_req(repos_raw, "scan", "[repos]"))
    if scan != "all":
        raise PolicyError(f"[repos]: scan must be 'all', got {scan!r}")

    return Policy(
        epoch_minutes=epoch_minutes,
        route_expiry_epochs=int(_req(epoch, "route_expiry_epochs", "[epoch]")),
        band_percentage_points=float(
            _req(allocation, "band_percentage_points", "[allocation]")
        ),
        tiebreak=tiebreak,
        slack=_fraction(_req(pacing, "slack", "[pacing]"), "[pacing]"),
        reserves=MappingProxyType(reserves),
        margins=Margins(
            floor_native_units=float(
                _req(margins_raw, "floor_native_units", "[margins]")
            ),
            peak_window_minutes=int(
                _req(margins_raw, "peak_window_minutes", "[margins]")
            ),
            peak_lookback_days=int(
                _req(margins_raw, "peak_lookback_days", "[margins]")
            ),
            stale_multiplier=float(_req(margins_raw, "stale_multiplier", "[margins]")),
        ),
        capacity=Capacity(
            archbox_min_idle_runners=int(
                _req(capacity_raw, "archbox_min_idle_runners", "[capacity]")
            ),
            max_start_latency_s=float(
                _req(capacity_raw, "max_start_latency_s", "[capacity]")
            ),
        ),
        circuit=Circuit(
            trip_infra_failures=int(
                _req(circuit_raw, "trip_infra_failures", "[circuit]")
            ),
            trip_window_minutes=int(
                _req(circuit_raw, "trip_window_minutes", "[circuit]")
            ),
            trip_read_failures=int(
                _req(circuit_raw, "trip_read_failures", "[circuit]")
            ),
            open_epochs=int(_req(circuit_raw, "open_epochs", "[circuit]")),
        ),
        allowances=MappingProxyType(
            {
                key: float(_req(allowances_raw, key, "[allowances]"))
                for key in (
                    "github_org_native_minutes",
                    "github_personal_native_minutes",
                    "blacksmith_native_minutes",
                )
            }
        ),
        providers=MappingProxyType(providers),
        families=MappingProxyType(families),
        repos=Repos(
            org=str(_req(repos_raw, "org", "[repos]")),
            scan=scan,
            personal_owner=str(_req(repos_raw, "personal_owner", "[repos]")),
            personal=tuple(_req(repos_raw, "personal", "[repos]")),
        ),
        ledger=LedgerPolicy(
            overlap_minutes=int(_req(ledger_raw, "overlap_minutes", "[ledger]")),
            safety_seconds=int(_req(ledger_raw, "safety_seconds", "[ledger]")),
        ),
        watchdog_max_state_age_epochs=int(
            _req(watchdog_raw, "max_state_age_epochs", "[watchdog]")
        ),
        native_rates=_rate_table(
            _req(norm_raw, "native", "[normalization]"), "[normalization.native]"
        ),
        normalized_rates=_rate_table(
            _req(norm_raw, "normalized", "[normalization]"),
            "[normalization.normalized]",
        ),
        github_os_multiplier=MappingProxyType(
            {
                family: float(
                    _req(
                        _req(norm_raw, "github_os_multiplier", "[normalization]"),
                        family,
                        "[normalization.github_os_multiplier]",
                    )
                )
                for family in FAMILIES
            }
        ),
        reconciliation=Reconciliation(
            hour_utc=int(_req(recon_raw, "hour_utc", "[reconciliation]")),
            drift_alert_fraction=_fraction(
                _req(recon_raw, "drift_alert_fraction", "[reconciliation]"),
                "[reconciliation]",
            ),
        ),
        rescue_enabled=bool(_req(rescue_raw, "enabled", "[rescue]")),
        rescue_max_queue_minutes=int(_req(rescue_raw, "max_queue_minutes", "[rescue]")),
    )
