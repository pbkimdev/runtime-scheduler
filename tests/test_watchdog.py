from __future__ import annotations

import json

import pytest

from scheduler import watchdog as wd
from tests.conftest import at

NOW = at("2026-09-11T18:00:00Z")


class FakeClient:
    def __init__(self, current):
        self.current = dict(current)
        self.writes: list[tuple[str, str]] = []

    def list_org_variables(self, org):
        return dict(self.current)

    def patch_org_variable(self, org, name, value):
        self.writes.append((name, value))
        self.current[name] = value


@pytest.mark.parametrize(
    ("state", "reset", "reason"),
    [
        (None, True, wd.NO_STATE),
        ({"status": "ok"}, True, wd.NO_EXPIRY),
        # One epoch of grace: 18:00 minus a 17:44 expiry is 16 minutes.
        ({"expires_at": "2026-09-11T17:44:00Z"}, True, wd.EXPIRED),
        ({"expires_at": "2026-09-11T17:50:00Z"}, False, wd.FRESH),
        ({"expires_at": "2026-09-11T18:45:00Z"}, False, wd.FRESH),
    ],
)
def test_decision_table(policy, state, reset, reason):
    decision = wd.decide(NOW, state, policy)
    assert decision.reset is reset
    assert decision.reason == reason


def test_a_fresh_state_leaves_every_variable_alone(policy):
    client = FakeClient(
        {
            "RUNTIME_STATE": json.dumps({"expires_at": "2026-09-11T18:45:00Z"}),
            "RUNTIME_ROUTE_LINUX_1": "archbox-linux-x64",
        }
    )
    code, result = wd.run(client, policy, NOW, dry_run=False)
    assert code == 0
    assert client.writes == []
    assert result.decision.reason == wd.FRESH


def test_an_expired_state_resets_routes_and_exits_red(policy):
    client = FakeClient(
        {
            "RUNTIME_STATE": json.dumps({"expires_at": "2026-09-11T16:00:00Z"}),
            "RUNTIME_ROUTE_LINUX_1": "archbox-linux-x64",
            "RUNTIME_ROUTE_LINUX_2": "ubuntu-latest",
            "RUNTIME_ROUTE_MACOS_1": "blacksmith-6vcpu-macos-latest",
        }
    )
    code, result = wd.run(client, policy, NOW, dry_run=False)
    assert code == 1
    assert "RUNTIME_ROUTE_LINUX_1" in result.written
    assert "RUNTIME_ROUTE_LINUX_2" in result.skipped  # already at its default
    written = dict(client.writes)
    assert written["RUNTIME_ROUTE_LINUX_1"] == "ubuntu-latest"
    assert written["RUNTIME_ROUTE_MACOS_1"] == "macos-latest"
    # The state is rewritten last, merged over the old one.
    assert client.writes[-1][0] == "RUNTIME_STATE"
    assert json.loads(client.writes[-1][1])["status"] == wd.RESET_STATUS


def test_a_reset_exits_red_even_when_no_route_moved(policy):
    """PLAN.md step 3: exit non-zero whenever a reset happened. Routes that
    already sit at their defaults do not make the allocator's silence less of
    a problem, and the red Actions tab is the whole signal."""
    current = dict(wd.default_routes(policy))
    current["RUNTIME_STATE"] = json.dumps({"status": "ok"})
    client = FakeClient(current)
    code, result = wd.run(client, policy, NOW, dry_run=False)
    assert code == 1
    assert result.written == []
    assert result.decision.reset is True
    # The state is still rewritten so the next tick sees the reset.
    assert client.writes[-1][0] == "RUNTIME_STATE"


def test_dry_run_never_writes(policy):
    current = dict(wd.default_routes(policy))
    current["RUNTIME_STATE"] = json.dumps({"expires_at": "2026-09-11T16:00:00Z"})
    current["RUNTIME_ROUTE_LINUX_1"] = "archbox-linux-x64"
    client = FakeClient(current)
    code, result = wd.run(client, policy, NOW, dry_run=True)
    assert client.writes == []
    assert result.written == ["RUNTIME_ROUTE_LINUX_1"]
    assert code == 1


def test_dry_run_still_reports_a_stale_state_red(policy):
    """A disabled-but-dispatched watchdog has to surface the condition even
    though it writes nothing."""
    current = dict(wd.default_routes(policy))
    current["RUNTIME_STATE"] = json.dumps({"expires_at": "2026-09-11T16:00:00Z"})
    client = FakeClient(current)
    code, result = wd.run(client, policy, NOW, dry_run=True)
    assert client.writes == []
    assert result.written == []
    assert code == 1
