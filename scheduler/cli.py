"""The `scheduler` command line.

The decision goes to stdout as JSON, a human summary to stderr, and a markdown
table to GITHUB_STEP_SUMMARY when the environment provides one.

Exit codes: 0 ok; 1 published, but the status is no_eligible_runtime or a
drift alert fired; 2 policy parse error, nothing written; 3 the publish failed
part way through and the message names what was written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import allocate as allocate_mod
from . import contract as contract_mod
from . import facts as facts_mod
from . import publish as publish_mod
from . import reconcile as reconcile_mod
from . import watchdog as watchdog_mod
from .github import GitHubClient, GitHubError
from .policy import PolicyError, load_policy

EXIT_OK = 0
EXIT_ALERT = 1
EXIT_POLICY = 2
EXIT_PUBLISH = 3

DEFAULT_POLICY = "policy.toml"
DEFAULT_LEDGER_DIR = "ledger"


def env(name: str) -> str | None:
    """An absent secret renders as an empty string in a workflow, so empty and
    unset are the same thing here."""
    value = os.environ.get(name, "").strip()
    return value or None


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return datetime.fromisoformat(value).astimezone(UTC)


def _client() -> GitHubClient:
    token = env("GH_TOKEN")
    if token is None:
        raise SystemExit("GH_TOKEN is required")
    return GitHubClient(token=token)


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def _route_rows(decision) -> list[str]:
    rows = ["| family | status | routes |", "|---|---|---|"]
    rows += [
        f"| {name} | {family.status} | {', '.join(family.routes)} |"
        for name, family in decision.families.items()
    ]
    rows += ["", "| family | provider | rejected because |", "|---|---|---|"]
    rows += [
        f"| {name} | {provider} | {reason} |"
        for name, family in decision.families.items()
        for provider, reason in sorted(family.rejected.items())
    ]
    return rows


def _budget_rows(state: dict) -> list[str]:
    rows = [
        "| provider | used | margin | cap (paced) | cap (hard) | circuit |",
        "|---|---|---|---|---|---|",
    ]
    rows += [
        f"| {provider} | {budget['used']} | {budget['margin']} | "
        f"{budget['cap']} | {budget['cap_hard']} | {state['circuits'][provider]} |"
        for provider, budget in state["budgets"].items()
    ]
    return rows


def _reconciliation_rows(results) -> list[str]:
    if not results:
        return []
    rows = [
        "| reconciliation | status | ledger | vendor | drift |",
        "|---|---|---|---|---|",
    ]
    rows += [
        f"| {result.source} | {result.status}{(' ' + result.reason).rstrip()} "
        f"| {result.ledger_total} | {result.vendor_total} | {result.drift} |"
        for result in results
    ]
    rows.append("")
    # Minutes the vendor reported for repositories the ledger does not bill:
    # public repositories, and repositories that no longer exist.
    rows += [
        f"- {result.source} did not compare {sum(result.unattributed.values()):.0f} "
        f"minutes from {', '.join(sorted(result.unattributed))}"
        for result in results
        if result.unattributed
    ]
    return [*rows, ""]


def _publish_rows(plans, dry_run: bool) -> list[str]:
    if not plans:
        return []
    rows = ["| variable | value | action |", "|---|---|---|"]
    for plan in plans:
        if not plan.changed:
            action = "unchanged"
        else:
            action = "would write" if dry_run else "write"
        value = plan.value if len(plan.value) < 60 else plan.value[:57] + "..."
        rows.append(f"| {plan.variable} | `{value}` | {action} |")
    return [*rows, ""]


def _markdown(state: dict, decision, facts, results, plans) -> str:
    lines = [f"## allocate {state['tick']} ({state['status']})", ""]
    if state["dry_run"]:
        lines += [
            "**dry run** - the ledger was built, no variable was written.",
            "",
        ]
    lines += _route_rows(decision)
    lines += ["", *_budget_rows(state), ""]
    lines += [
        f"Staleness `{state['staleness']}`, cursor `{state['cursor']}`, "
        f"cursor age {facts.cursor_age_s:.0f}s. "
        f"Scanned {facts.repos_scanned} repositories, {facts.runs_scanned} runs, "
        f"added {facts.jobs_added} jobs.",
        "",
    ]
    lines += _reconciliation_rows(results)
    lines += _publish_rows(plans, state["dry_run"])
    lines += [f"- warning: {warning}" for warning in facts.warnings]
    return "\n".join(lines) + "\n"


def cmd_allocate(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"policy error: {exc}", file=sys.stderr)
        return EXIT_POLICY

    now = _now(args.now)
    client = _client()
    ledger_dir = Path(args.ledger_dir)

    facts = facts_mod.collect_facts(now, policy, client, ledger_dir)
    decision = allocate_mod.allocate(now, policy, facts)

    results: list[reconcile_mod.SourceResult] = []
    drift_alert = False
    if args.reconcile or reconcile_mod.should_reconcile(
        now, facts.ledger, policy.reconciliation.hour_utc
    ):
        threshold = policy.reconciliation.drift_alert_fraction
        results.append(
            reconcile_mod.reconcile_github(
                client,
                facts.ledger,
                now,
                policy.repos.org,
                personal=False,
                threshold=threshold,
                private_repos=facts.private_repos,
            )
        )
        personal_token = env("GH_PERSONAL_BILLING_TOKEN")
        if personal_token is None:
            results.append(
                reconcile_mod.SourceResult(
                    source="github_personal",
                    status=reconcile_mod.STATUS_SKIPPED,
                    reason="no_token",
                )
            )
        else:
            results.append(
                reconcile_mod.reconcile_github(
                    GitHubClient(token=personal_token),
                    facts.ledger,
                    now,
                    policy.repos.personal_owner,
                    personal=True,
                    threshold=threshold,
                )
            )
        results.append(
            reconcile_mod.reconcile_blacksmith(
                facts.ledger,
                token=env("BLACKSMITH_TOKEN"),
                threshold=threshold,
                owner=policy.repos.org,
            )
        )
        drift_alert = reconcile_mod.apply_results(
            facts.ledger,
            results,
            now,
            github_os_multiplier=policy.github_os_multiplier,
            org=policy.repos.org,
        )

    skipped = [r.source for r in results if r.status == reconcile_mod.STATUS_SKIPPED]
    state = allocate_mod.state_json(
        decision,
        facts,
        policy,
        dry_run=args.dry_run,
        drift_alert=drift_alert,
        reconciliation_skipped=skipped,
    )

    # The ledger is written in both modes. Dry-run accumulates the history the
    # first paced month needs; only the variables are withheld.
    facts.ledger.write(ledger_dir)

    try:
        current = client.list_org_variables(policy.repos.org)
    except GitHubError as exc:
        current = {}
        facts.warnings.append(f"variable read failed, treating all as changed: {exc}")

    routes = {name: family.routes for name, family in decision.families.items()}
    exit_code = EXIT_OK
    try:
        result = publish_mod.publish(
            client,
            policy.repos.org,
            routes,
            state,
            dry_run=args.dry_run,
            current=current,
        )
        plans = result.plans
    except publish_mod.PublishFailed as exc:
        plans = []
        print(str(exc), file=sys.stderr)
        exit_code = EXIT_PUBLISH

    print(json.dumps(state, separators=(",", ":"), sort_keys=True))
    _write_summary(_markdown(state, decision, facts, results, plans))

    for warning in facts.warnings:
        print(f"::warning::{warning}", file=sys.stderr)
    print(
        f"status={decision.status} staleness={facts.staleness} "
        f"jobs_added={facts.jobs_added} dry_run={args.dry_run}",
        file=sys.stderr,
    )
    for name, family in decision.families.items():
        print(f"  {name}: {' '.join(family.routes)} ({family.status})", file=sys.stderr)

    if exit_code != EXIT_OK:
        return exit_code
    if decision.status == allocate_mod.STATUS_NONE or drift_alert:
        return EXIT_ALERT
    return EXIT_OK


def cmd_watchdog(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"policy error: {exc}", file=sys.stderr)
        return EXIT_POLICY

    now = _now(args.now)
    client = _client()
    code, result = watchdog_mod.run(client, policy, now, dry_run=args.dry_run)
    payload = {
        "reset": result.decision.reset,
        "reason": result.decision.reason,
        "age_s": result.decision.age_s,
        "written": result.written,
        "skipped": result.skipped,
        "dry_run": result.dry_run,
    }
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    _write_summary(
        f"## watchdog {result.decision.reason}\n\n"
        f"reset={result.decision.reset} written={result.written}\n"
    )
    if result.decision.reset and not result.written:
        print(
            "::warning::state was stale but every route already sat at its default",
            file=sys.stderr,
        )
    return code


def cmd_contract_check(args: argparse.Namespace) -> int:
    owner, _, repo = args.repo.partition("/")
    client = _client()
    result = contract_mod.check(client, owner, repo, timeout_s=args.timeout_s)
    print(
        json.dumps(
            {
                "ok": result.ok,
                "run_id": result.run_id,
                "run_url": result.run_url,
                "conclusion": result.conclusion,
                "observed": result.observed,
                "mismatches": result.mismatches,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    _write_summary(
        f"## contract-check {'ok' if result.ok else 'FAILED'}\n\n"
        f"{result.run_url}\n\n" + "".join(f"- {line}\n" for line in result.mismatches)
    )
    if not result.ok:
        print(result.message or "contract check failed", file=sys.stderr)
        return EXIT_ALERT
    return EXIT_OK


def cmd_show_state(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"policy error: {exc}", file=sys.stderr)
        return EXIT_POLICY
    client = _client()
    raw = client.get_org_variable(policy.repos.org, facts_mod.STATE_VARIABLE)
    if not raw:
        print("RUNTIME_STATE is not set", file=sys.stderr)
        return EXIT_ALERT
    print(json.dumps(json.loads(raw), indent=2, sort_keys=True))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scheduler")
    sub = parser.add_subparsers(dest="command", required=True)

    allocate = sub.add_parser("allocate", help="compute and publish routes")
    allocate.add_argument("--policy", default=DEFAULT_POLICY)
    allocate.add_argument("--dry-run", action="store_true")
    allocate.add_argument("--now", default=None, help="ISO timestamp, for tests")
    allocate.add_argument("--ledger-dir", default=DEFAULT_LEDGER_DIR)
    allocate.add_argument(
        "--reconcile", action="store_true", help="force reconciliation this tick"
    )
    allocate.set_defaults(func=cmd_allocate)

    watchdog = sub.add_parser("watchdog", help="reset routes when the state is stale")
    watchdog.add_argument("--policy", default=DEFAULT_POLICY)
    watchdog.add_argument("--dry-run", action="store_true")
    watchdog.add_argument("--now", default=None)
    watchdog.set_defaults(func=cmd_watchdog)

    contract = sub.add_parser(
        "contract-check", help="verify the E4a job-output contract"
    )
    contract.add_argument("--repo", default="pbkimdev/runtime-scheduler")
    contract.add_argument("--timeout-s", type=int, default=900)
    contract.set_defaults(func=cmd_contract_check)

    show = sub.add_parser("show-state", help="pretty-print RUNTIME_STATE")
    show.add_argument("--policy", default=DEFAULT_POLICY)
    show.set_defaults(func=cmd_show_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
