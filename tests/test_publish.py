from __future__ import annotations

import pytest

from scheduler import publish as pub
from scheduler.github import GitHubError

ROUTES = {
    "linux": ("archbox-linux-x64", "blacksmith-4vcpu-ubuntu-2404", "ubuntu-latest"),
    "windows": ("windows-latest", "blacksmith-4vcpu-windows-2025"),
    "macos": ("macos-latest", "blacksmith-6vcpu-macos-latest"),
}
STATE = {"status": "ok", "schema": 1}


class FakeClient:
    def __init__(self, current=None, fail_on=None):
        self.current = dict(current or {})
        self.fail_on = fail_on
        self.writes: list[tuple[str, str]] = []

    def list_org_variables(self, org):
        return dict(self.current)

    def patch_org_variable(self, org, name, value):
        if name == self.fail_on:
            raise GitHubError(403, name, "forbidden")
        self.writes.append((name, value))


def test_routes_are_written_before_the_state_and_the_state_is_last():
    client = FakeClient()
    result = pub.publish(client, "pbkimdev", ROUTES, STATE, dry_run=False)
    assert [name for name, _ in client.writes] == [
        "RUNTIME_ROUTE_LINUX_1",
        "RUNTIME_ROUTE_LINUX_2",
        "RUNTIME_ROUTE_LINUX_3",
        "RUNTIME_ROUTE_WINDOWS_1",
        "RUNTIME_ROUTE_WINDOWS_2",
        "RUNTIME_ROUTE_MACOS_1",
        "RUNTIME_ROUTE_MACOS_2",
        "RUNTIME_STATE",
    ]
    assert result.written[-1] == "RUNTIME_STATE"


def test_an_unchanged_route_is_not_patched():
    client = FakeClient(
        current={
            "RUNTIME_ROUTE_LINUX_1": "archbox-linux-x64",
            "RUNTIME_ROUTE_MACOS_2": "blacksmith-6vcpu-macos-latest",
        }
    )
    result = pub.publish(client, "pbkimdev", ROUTES, STATE, dry_run=False)
    assert "RUNTIME_ROUTE_LINUX_1" in result.skipped
    assert "RUNTIME_ROUTE_MACOS_2" in result.skipped
    assert "RUNTIME_ROUTE_LINUX_1" not in [name for name, _ in client.writes]
    # The state is rewritten every tick even when nothing else moved, because
    # its expiry is what the watchdog reads.
    assert "RUNTIME_STATE" in result.written


def test_dry_run_writes_nothing_but_reports_every_value():
    client = FakeClient()
    result = pub.publish(client, "pbkimdev", ROUTES, STATE, dry_run=True)
    assert client.writes == []
    assert result.written == []
    assert [plan.value for plan in result.plans][:3] == list(ROUTES["linux"])


def test_a_partial_publish_names_what_was_written():
    client = FakeClient(fail_on="RUNTIME_ROUTE_WINDOWS_1")
    with pytest.raises(pub.PublishFailed) as caught:
        pub.publish(client, "pbkimdev", ROUTES, STATE, dry_run=False)
    assert caught.value.variable == "RUNTIME_ROUTE_WINDOWS_1"
    assert caught.value.written == [
        "RUNTIME_ROUTE_LINUX_1",
        "RUNTIME_ROUTE_LINUX_2",
        "RUNTIME_ROUTE_LINUX_3",
    ]


def test_the_family_argument_is_typed():
    broken = dict(ROUTES, macos=("macos-latest",))
    with pytest.raises(ValueError, match="macos produced 1 routes"):
        pub.publish(FakeClient(), "pbkimdev", broken, STATE, dry_run=True)


def test_an_oversized_state_is_refused_before_anything_is_written():
    client = FakeClient()
    huge = {"status": "ok", "padding": "x" * (pub.MAX_STATE_BYTES + 1)}
    with pytest.raises(pub.StateTooLarge):
        pub.publish(client, "pbkimdev", ROUTES, huge, dry_run=False)
    assert client.writes == []
