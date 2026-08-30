"""The E4a contract test.

The fallback chain rests on one undocumented behavior: job outputs survive a
failed job. Phase 0 observed it (E4a, run 33304844667) but GitHub never
documented it. This check dispatches contract-probe.yml and asserts the
observed shape, so the day GitHub changes it we find out from a weekly red
run rather than from a fallback job that silently stops firing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .github import GitHubClient

PROBE_WORKFLOW = "contract-probe.yml"

# job name -> expected conclusion
EXPECTED = {
    "a": "failure",
    "guard_a": "skipped",
    "witness_a": "success",
    "witness_b": "success",
}

MESSAGE = (
    "GitHub changed the undocumented E4a behavior: job outputs no longer "
    "survive a failed job as observed at Gate 0. The fallback chain in "
    "linux.yml, windows.yml and macos.yml rests on it. See run {url}"
)


@dataclass
class ContractResult:
    ok: bool
    run_id: int | None = None
    run_url: str = ""
    conclusion: str = ""
    observed: dict[str, str] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)
    message: str = ""


def _find_run(
    client: GitHubClient, owner: str, repo: str, after: datetime
) -> dict | None:
    for run in client.list_workflow_runs(owner, repo, PROBE_WORKFLOW):
        created = datetime.fromisoformat(run["created_at"]).astimezone(UTC)
        if run.get("event") == "workflow_dispatch" and created >= after:
            return run
    return None


def check(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    ref: str = "main",
    timeout_s: int = 900,
    poll_s: int = 15,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> ContractResult:
    started = now() - timedelta(seconds=30)
    client.dispatch_workflow(owner, repo, PROBE_WORKFLOW, ref)

    deadline = now() + timedelta(seconds=timeout_s)
    run = None
    while now() < deadline:
        run = _find_run(client, owner, repo, started)
        if run is not None and run.get("status") == "completed":
            break
        sleep(poll_s)
    if run is None:
        return ContractResult(
            ok=False, message="contract probe run never appeared after dispatch"
        )
    if run.get("status") != "completed":
        return ContractResult(
            ok=False,
            run_id=run["id"],
            run_url=run.get("html_url", ""),
            message=f"contract probe did not finish within {timeout_s}s",
        )

    jobs = client.list_jobs(owner, repo, int(run["id"]))
    observed = {job["name"]: job.get("conclusion") or "" for job in jobs}
    mismatches = [
        f"{name}: expected {expected}, observed {observed.get(name, 'absent')}"
        for name, expected in EXPECTED.items()
        if observed.get(name) != expected
    ]
    url = run.get("html_url", "")
    return ContractResult(
        ok=not mismatches,
        run_id=int(run["id"]),
        run_url=url,
        conclusion=run.get("conclusion") or "",
        observed=observed,
        mismatches=mismatches,
        message="" if not mismatches else MESSAGE.format(url=url),
    )
