# runtime-scheduler

RuntimeScheduler decides which runner pool `pbkimdev`'s CI jobs land on, and
publishes the answer as organization Actions variables. A caller workflow names
a reusable workflow and passes a command. It names no runner, no provider, and
no quota.

The whole dispatch channel is a variable read inside a `runs-on` expression.
There is no controller, no webhook, no service endpoint, and no per-job
reservation. An allocator outage leaves the last routes in force; a deleted
variable sends everything to GitHub-hosted runners.

## The caller contract

```yaml
# .github/workflows/ci.yml in any pbkimdev repository
name: ci
on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  linux:
    uses: pbkimdev/runtime-scheduler/.github/workflows/linux.yml@main
    with:
      run: scripts/ci-verify.sh
      side_effect: none
      timeout_minutes: 30
```

| Input | Meaning |
|---|---|
| `run` | One shell command. It runs as `bash -euo pipefail -c "$WORK_COMMAND"` in a step named `work`. Multi-step work belongs in a script in your repository |
| `side_effect` | `none`, `idempotent`, or `external`. Default `external`. Only `none` gets fallback jobs |
| `timeout_minutes` | Per attempt. Default 30 |

`linux.yml` has three jobs (`primary`, `fallback`, `last`); `windows.yml` and
`macos.yml` have two, because those families have two route slots.

A `fallback` job runs only when the previous attempt failed at the
infrastructure level: the job failed while its `work` step never reported
`success` or `failure`. A test that fails reports `failure`, so no other
provider repeats it. A checkout that fails reports `skipped`, which is
infrastructure and does get one retry elsewhere.

Side-effecting work uses `release.yml` instead. It has one job on a literal
`ubuntu-latest`, no route variable, and no fallback job, so a publish can never
run twice.

```yaml
  publish:
    uses: pbkimdev/runtime-scheduler/.github/workflows/release.yml@main
    with:
      run: scripts/publish.sh
    secrets:
      PUBLISH_TOKEN: ${{ secrets.REGISTRY_TOKEN }}
```

Pin the `@` reference to a full commit SHA in production callers.

## The disable path

Every level can be switched off without touching a caller.

| To stop | Do this |
|---|---|
| One route | `gh variable set RUNTIME_ROUTE_LINUX_1 --org pbkimdev --body ubuntu-latest` |
| All routing | `gh variable set ALLOCATE_MODE --repo pbkimdev/runtime-scheduler --body dry-run`. The allocator keeps building the ledger and writes no variable |
| The allocator entirely | `gh workflow disable allocate.yml --repo pbkimdev/runtime-scheduler`. The last routes stay in force |
| The watchdog | `gh workflow disable watchdog.yml --repo pbkimdev/runtime-scheduler` |
| Everything, from a caller's side | Delete the org variables. Every expression falls through to its `\|\| 'ubuntu-latest'` tail |

A personal repository cannot read organization variables at all, so it always
takes the bootstrap path. So does a fork pull request (E10: a fork sees no
variables), and the `github.event.repository.private && ... fork != true` guard
in the expression is the belt to that braces.

## The state variable

`RUNTIME_STATE` is one JSON object explaining the tick that wrote the routes.

```json
{
  "schema": 1,
  "status": "ok",
  "tick": "2026-09-11T18:00:00Z",
  "expires_at": "2026-09-11T18:45:00Z",
  "families": {
    "linux": {
      "status": "ok",
      "routes": ["archbox-linux-x64", "blacksmith-4vcpu-ubuntu-2404", "ubuntu-latest"],
      "deficits": {"github": -0.00556, "blacksmith": 0.00556, "archbox": 0.0},
      "shares": {"github": 0.33889, "blacksmith": 0.32778, "archbox": 0.33333}
    }
  },
  "rejected": {"linux": {"blacksmith": "pacing_exceeded"}},
  "budgets": {"github": {"used": 610, "margin": 84, "cap": 915.0, "cap_hard": 1800.0}},
  "cursor": "2026-09-11T17:58:31Z",
  "staleness": "normal",
  "circuits": {"github": "closed", "blacksmith": "closed", "archbox": "closed"},
  "reads": [{"tick": "2026-09-11T17:45:00Z", "github": true, "blacksmith": true, "archbox": true}],
  "pressure": {"github": 12.0, "blacksmith": null, "archbox": null},
  "drift_alert": false,
  "reconciliation_skipped": [],
  "dry_run": false
}
```

| Field | Meaning |
|---|---|
| `status` | `ok`, `pacing_exceeded` when nothing was primary-eligible, `no_eligible_runtime` when nothing was eligible at all, `watchdog_reset` when the watchdog reset the routes |
| `expires_at` | Three epochs out. The watchdog resets routes when this is more than one epoch in the past |
| `rejected` | Every provider dropped, with the reason, before any deficit was consulted |
| `budgets` | Native units spent, the margin that covers the 30-minute blind spot, the paced cap, and the hard cap |
| `staleness` | `normal`, `degraded` (margins doubled), `stale` (hosted providers become fallback-only) |
| `circuits` | `closed`, `open`, `half_open`. Derived each tick from the ledger, never stored |
| `reads` | The last two ticks' read outcomes per provider. Two consecutive failures reject a provider |
| `pressure` | Rolling 30-minute median job start latency, in seconds |

Reject reasons: `not_live`, `circuit_open`, `circuit_half_open`,
`unhealthy_reads`, `no_idle_capacity`, `queue_pressure`, `pacing_exceeded`,
`reserve_exceeded`, `stale_ledger`. Only `circuit_half_open`,
`pacing_exceeded`, and `stale_ledger` leave a provider fallback-eligible.

`scheduler show-state` pretty-prints the live value.

## Running locally

```bash
make dev
make verify                       # the whole gate, via ./scripts/verify
make lint                         # just ruff
make test                         # just pytest
GH_TOKEN=$(gh auth token) uv run scheduler allocate \
  --dry-run --ledger-dir /tmp/ledger
```

`make verify` runs `./scripts/verify`: gitleaks, ruff format and check (with
C901 and PLR0912 as the complexity gate), pytest, vulture, pip-audit, Biome on
JSON, yamlfmt and yamllint on YAML, and shellcheck and shfmt on shell.
`mise.toml` pins the non-Python tools; `uvx` fetches vulture and pip-audit.

A dry run reads the organization, builds the ledger, prints the decision as
JSON on stdout and a summary on stderr, and writes no variable. `--now ISO`
pins the clock. `--reconcile` forces the daily vendor comparison.

`make allocate-dry` is the same thing against the repository's own `ledger/`.

## Modes

The allocate workflow publishes only when the dispatch input says `publish`,
or when a scheduled run finds `vars.ALLOCATE_MODE == 'publish'`. Anything else
is a dry run.

**A dry run still builds and commits the ledger.** That is what accumulates the
14 days of history the first paced month needs, and it is what keeps GitHub
from disabling the scheduled workflows after 60 days of no repository activity.
Only the variables are withheld.

## What is live today

Only GitHub. `policy.toml` carries `[providers.blacksmith] live = false` and
`[providers.archbox] live = false`, so both are rejected with `not_live` before
anything else and never land in a route. With one live provider every route is
its family default. Blacksmith flips on when Paul supplies the written answer
on free-tier exhaustion and `BLACKSMITH_USAGE_TOKEN`; Archbox flips on in
Phase 2 with the disposable-guest warm pool.

The GitHub hard reserve sits at 0.20 rather than the plan's calibrated 0.10
until experiment E9b observes what a $0 stop budget does to an exhausted
allowance.

## Workflows in this repository

| File | What it does |
|---|---|
| `linux.yml`, `windows.yml`, `macos.yml` | The reusable workflows callers name. The route expression lives here; callers pass nothing |
| `release.yml` | One job, literal `ubuntu-latest`, no fallback. Side-effecting work |
| `allocate.yml` | Every 15 minutes: build the ledger, decide, publish, commit |
| `watchdog.yml` | Offset by half an epoch: reset routes when the decision expired |
| `contract.yml` | Weekly: prove the undocumented E4a job-output behavior still holds |
| `contract-probe.yml` | The red-by-design probe `contract.yml` dispatches |
| `ci.yml` | `make verify` on push and pull request. The scheduler does not route itself |

## Layout

```
policy.toml        every knob: reserves, allowances, rates, labels, liveness
scheduler/         the package. No runtime dependencies
ledger/            ledger/YYYY-MM.json, committed by the allocate workflow
tests/             pytest
scripts/verify     the gate every other check runs through
scripts/e1*.sh     Phase 0 leftovers
```
