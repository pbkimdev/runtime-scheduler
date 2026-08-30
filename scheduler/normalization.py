"""Native and normalized unit rates, looked up by runner label.

Native units answer "how much of this provider's allowance did we spend".
Normalized units answer "how much compute did each provider do for us" and
exist only to compute allocation shares. The two are never mixed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from fnmatch import fnmatchcase

UNKNOWN_LABEL_RATE = 1.0


def rate_for(
    label: str,
    table: Sequence[tuple[str, float]],
    warnings: list[str] | None = None,
    kind: str = "rate",
) -> float:
    """First matching glob in document order wins."""
    for pattern, rate in table:
        if fnmatchcase(label, pattern):
            return rate
    if warnings is not None:
        warnings.append(f"unknown runner label {label!r}: using {kind} 1")
    return UNKNOWN_LABEL_RATE


def rounded_minutes(seconds: float) -> int:
    """GitHub rounds each job's minutes up to a whole minute (evidence #7).

    The ledger inherits that rule for every provider so the two ledgers are
    computed the same way and only the rate differs.
    """
    if seconds <= 0:
        return 0
    return math.ceil(seconds / 60)


def native_units(
    *,
    provider: str,
    family: str,
    label: str,
    seconds: float,
    private: bool,
    policy,
    warnings: list[str] | None = None,
) -> float:
    """Native units for one finished job.

    GitHub bills nothing for a public repository, so a public job contributes
    zero even though it consumed real wall time. Archbox has no allowance and
    its rate table entry is zero.
    """
    minutes = rounded_minutes(seconds)
    if provider == "github":
        if not private:
            return 0.0
        return minutes * policy.github_os_multiplier[family]
    return minutes * rate_for(label, policy.native_rates, warnings, "native rate")


def normalized_units(
    *,
    label: str,
    seconds: float,
    policy,
    warnings: list[str] | None = None,
) -> float:
    minutes = rounded_minutes(seconds)
    return minutes * rate_for(
        label, policy.normalized_rates, warnings, "normalized rate"
    )
