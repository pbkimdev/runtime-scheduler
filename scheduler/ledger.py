"""The month ledger: the scheduler's own record of consumed compute.

The ledger is the primary quota source (ADR 0008). It is built from the jobs
API, keyed on (run_id, run_attempt, job_id) so a re-run attempt is a new
allocation (E15), and committed to the repository so the audit trail and the
scheduled-workflow activity both come free.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .normalization import native_units, normalized_units, rounded_minutes

SCHEMA = 1
ROUTED_SUFFIXES = ("/ primary", "/ fallback", "/ last")
WORK_STEP_NAME = "work"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def format_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class JobRecord:
    key: str
    repo: str
    private: bool
    name: str
    labels: tuple[str, ...]
    provider: str
    family: str
    routed: bool
    event: str
    created_at: str
    started_at: str
    completed_at: str
    conclusion: str
    work: str
    infra: bool
    native: float
    normalized: float
    latency_s: float | None

    @property
    def completed(self) -> datetime:
        return parse_ts(self.completed_at)  # type: ignore[return-value]

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def wall_seconds(self) -> float:
        started = parse_ts(self.started_at)
        completed = parse_ts(self.completed_at)
        if started is None or completed is None:
            return 0.0
        return max(0.0, (completed - started).total_seconds())


def job_key(run_id: int, run_attempt: int, job_id: int) -> str:
    return f"{run_id}:{run_attempt}:{job_id}"


def provider_for(labels: Sequence[str]) -> str:
    for label in labels:
        lowered = label.lower()
        if lowered.startswith("blacksmith-"):
            return "blacksmith"
        if lowered.startswith("archbox-"):
            return "archbox"
    return "github"


def family_for(labels: Sequence[str]) -> str:
    for label in labels:
        lowered = label.lower()
        if "windows" in lowered:
            return "windows"
        if "macos" in lowered:
            return "macos"
    return "linux"


def work_outcome(steps: Iterable[dict[str, Any]]) -> str:
    """`success`, `failure`, `skipped`, `cancelled`, or `none` when the job
    carries no step named `work` at all."""
    for step in steps or ():
        if step.get("name") == WORK_STEP_NAME:
            return step.get("conclusion") or "none"
    return "none"


def is_infra_failure(conclusion: str, work: str, routed: bool) -> bool:
    """An infrastructure failure is a job that failed while its work step never
    reported success or failure (ADR 0007).

    A job with no `work` step at all is only judged when it is one of ours; a
    hand-written job that failed is a workload failure by default, because
    there is no contract saying otherwise.
    """
    if conclusion != "failure":
        return False
    if work in ("success", "failure"):
        return False
    if work == "none":
        return routed
    return True


def is_fork_pull_request(run: dict[str, Any]) -> bool:
    if run.get("event") != "pull_request":
        return False
    head = (run.get("head_repository") or {}).get("full_name")
    base = (run.get("repository") or {}).get("full_name")
    if head is None or base is None:
        return False
    return head != base


def build_record(
    *,
    job: dict[str, Any],
    run: dict[str, Any],
    repo_full_name: str,
    private: bool,
    policy,
    warnings: list[str] | None = None,
) -> JobRecord | None:
    """None when the job has not finished; it is picked up on a later tick."""
    started = parse_ts(job.get("started_at"))
    completed = parse_ts(job.get("completed_at"))
    if completed is None or started is None:
        return None

    created = parse_ts(job.get("created_at")) or started
    labels = tuple(job.get("labels") or ())
    provider = provider_for(labels)
    family = family_for(labels)
    name = job.get("name") or ""
    routed = (
        any(name.endswith(suffix) for suffix in ROUTED_SUFFIXES)
        and private
        and not is_fork_pull_request(run)
    )
    seconds = max(0.0, (completed - started).total_seconds())
    conclusion = job.get("conclusion") or ""
    work = work_outcome(job.get("steps") or ())
    label = labels[0] if labels else ""

    return JobRecord(
        key=job_key(
            int(run.get("id", job.get("run_id", 0))),
            int(job.get("run_attempt", 1)),
            int(job["id"]),
        ),
        repo=repo_full_name,
        private=private,
        name=name,
        labels=labels,
        provider=provider,
        family=family,
        routed=routed,
        event=run.get("event") or "",
        created_at=format_ts(created),
        started_at=format_ts(started),
        completed_at=format_ts(completed),
        conclusion=conclusion,
        work=work,
        infra=is_infra_failure(conclusion, work, routed),
        native=native_units(
            provider=provider,
            family=family,
            label=label,
            seconds=seconds,
            private=private,
            policy=policy,
            warnings=warnings,
        ),
        normalized=normalized_units(
            label=label, seconds=seconds, policy=policy, warnings=warnings
        ),
        latency_s=max(0.0, (started - created).total_seconds()),
    )


@dataclass
class MonthLedger:
    month: str
    cursor: str
    reconciled_on: str | None = None
    # While now is before this, every margin doubles. Reconciliation sets it
    # when drift passes the threshold; it is persisted so the doubling covers
    # the 96 ticks in the next 24 hours rather than one tick.
    drift_alert_until: str | None = None
    jobs: dict[str, JobRecord] = field(default_factory=dict)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    reconciliations: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = False
    # The cursor as it stands in the committed file. `cursor` moves during a
    # tick; this does not, so the commit heartbeat measures against disk.
    loaded_cursor: str = ""

    def __post_init__(self) -> None:
        if not self.loaded_cursor:
            self.loaded_cursor = self.cursor

    # ---- persistence ---------------------------------------------------

    @classmethod
    def path_for(cls, ledger_dir: Path | str, month: str) -> Path:
        return Path(ledger_dir) / f"{month}.json"

    @classmethod
    def load(
        cls, ledger_dir: Path | str, month: str, month_start: datetime
    ) -> MonthLedger:
        """A fresh month starts a new file whose cursor is the month start."""
        path = cls.path_for(ledger_dir, month)
        if not path.exists():
            return cls(month=month, cursor=format_ts(month_start))
        raw = json.loads(path.read_text(encoding="utf-8"))
        jobs = {}
        for entry in raw.get("jobs", []):
            entry = dict(entry)
            entry["labels"] = tuple(entry.get("labels", ()))
            record = JobRecord(**entry)
            jobs[record.key] = record
        return cls(
            month=raw.get("month", month),
            cursor=raw.get("cursor", format_ts(month_start)),
            reconciled_on=raw.get("reconciled_on"),
            drift_alert_until=raw.get("drift_alert_until"),
            jobs=jobs,
            corrections=raw.get("corrections", []),
            reconciliations=raw.get("reconciliations", []),
        )

    def dumps(self) -> str:
        head = {
            "schema": SCHEMA,
            "month": self.month,
            "cursor": self.cursor,
            "reconciled_on": self.reconciled_on,
            "drift_alert_until": self.drift_alert_until,
            "corrections": self.corrections,
            "reconciliations": self.reconciliations,
        }
        parts = [
            f"{json.dumps(key)}:"
            f"{json.dumps(head[key], separators=(',', ':'), sort_keys=True)}"
            for key in sorted(head)
        ]
        records = [
            json.dumps(asdict(record), separators=(",", ":"), sort_keys=True)
            for record in sorted(
                self.jobs.values(), key=lambda r: (r.completed_at, r.key)
            )
        ]
        body = ",\n".join(parts)
        if records:
            jobs_block = '"jobs":[\n' + ",\n".join(records) + "\n]"
        else:
            jobs_block = '"jobs":[]'
        return "{\n" + body + ",\n" + jobs_block + "\n}\n"

    def write(self, ledger_dir: Path | str) -> Path:
        path = self.path_for(ledger_dir, self.month)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8")
        return path

    # ---- accumulation ---------------------------------------------------

    def add(self, record: JobRecord) -> bool:
        """False when the job is already recorded. Dedup is by key, so the same
        job seen through the overlap window twice lands once."""
        if record.key in self.jobs:
            return False
        self.jobs[record.key] = record
        self.dirty = True
        return True

    def drift_alert_active(self, now: datetime) -> bool:
        until = parse_ts(self.drift_alert_until)
        return until is not None and now < until

    def adopt_cursor(self, value: str | None, month_start: datetime) -> None:
        """Take a cursor a previous publishing tick recorded in RUNTIME_STATE.

        A publish-mode allocator therefore never rescans more than the overlap,
        even when the ledger file lags behind because the commit heartbeat has
        not fired yet. Never rewinds, and never reaches into a previous month.
        """
        candidate = parse_ts(value)
        current = parse_ts(self.cursor)
        if candidate is None or candidate < month_start:
            return
        if current is not None and candidate <= current:
            return
        self.cursor = format_ts(candidate)

    def advance_cursor(
        self,
        safety_seconds: int,
        *,
        scanned_through: datetime | None = None,
        oldest_inflight: datetime | None = None,
    ) -> None:
        """Newest completed_at seen, minus the safety step. Never rewinds.

        `scanned_through` is the tick time of a scan that read every repository
        without a failure. Without it a quiet organization would keep an old
        cursor and declare its own accounting stale, which is the opposite of
        what the staleness rule is for. `oldest_inflight` clamps the cursor
        behind the oldest run still in progress so a long job is not skipped
        past before it finishes.
        """
        candidates: list[datetime] = []
        if self.jobs:
            candidates.append(max(record.completed for record in self.jobs.values()))
        if scanned_through is not None:
            candidates.append(scanned_through)
        if not candidates:
            return
        candidate = max(candidates) - timedelta(seconds=safety_seconds)
        if oldest_inflight is not None:
            candidate = min(
                candidate, oldest_inflight - timedelta(seconds=safety_seconds)
            )
        current = parse_ts(self.cursor)
        if current is None or candidate > current:
            self.cursor = format_ts(candidate)
            self.dirty = True

    # ---- queries ---------------------------------------------------------

    def records(self) -> list[JobRecord]:
        return list(self.jobs.values())

    def native_used(self, provider: str, owner: str | None = None) -> float:
        """Native units spent against one owner's allowance.

        The organization and personal pools are separate (E7), so a budget
        check must not add a personal repository's minutes to the org's.
        """
        total = sum(
            record.native
            for record in self.jobs.values()
            if record.provider == provider and (owner is None or record.owner == owner)
        )
        for correction in self.corrections:
            if correction.get("provider") == provider and (
                owner is None or correction.get("owner") == owner
            ):
                total += float(correction.get("delta", 0.0))
        return total

    def normalized_by_provider(
        self, family: str, providers: Sequence[str]
    ) -> dict[str, float]:
        """Only routed jobs count toward a family denominator. A public repo,
        a personal repo, or a fork PR had no choice of provider."""
        totals = {provider: 0.0 for provider in providers}
        for record in self.jobs.values():
            if record.family != family or not record.routed:
                continue
            if record.provider in totals:
                totals[record.provider] += record.normalized
        return totals

    def raw_minutes_by_family(
        self,
        owner: str | None = None,
        provider: str = "github",
        *,
        private_only: bool = True,
    ) -> dict[str, float]:
        """Rounded wall minutes per OS family, before any OS multiplier.

        This is the quantity the vendor billing report counts, so it is what
        reconciliation compares against. GitHub bills nothing for a public
        repository; Blacksmith bills regardless of visibility, so its caller
        passes private_only=False.
        """
        totals: dict[str, float] = {}
        for record in self.jobs.values():
            if record.provider != provider:
                continue
            if owner is not None and record.owner != owner:
                continue
            if private_only and not record.private:
                continue
            totals[record.family] = totals.get(record.family, 0.0) + rounded_minutes(
                record.wall_seconds
            )
        return totals

    def infra_times(self, provider: str) -> list[datetime]:
        return sorted(
            record.completed
            for record in self.jobs.values()
            if record.provider == provider and record.infra
        )

    def peak30(
        self, provider: str, now: datetime, window_minutes: int, lookback_days: int
    ) -> float:
        """Max native units in any window_minutes window over the lookback."""
        floor = now - timedelta(days=lookback_days)
        points = sorted(
            (record.completed, record.native)
            for record in self.jobs.values()
            if record.provider == provider and record.completed >= floor
        )
        if not points:
            return 0.0
        window = timedelta(minutes=window_minutes)
        peak = 0.0
        for index, (start, _) in enumerate(points):
            total = 0.0
            for stamp, native in points[index:]:
                if stamp - start >= window:
                    break
                total += native
            peak = max(peak, total)
        return peak

    def median_latency_s(
        self, provider: str, now: datetime, window_minutes: int
    ) -> float | None:
        floor = now - timedelta(minutes=window_minutes)
        values = [
            record.latency_s
            for record in self.jobs.values()
            if record.provider == provider
            and record.latency_s is not None
            and (parse_ts(record.started_at) or now) >= floor
        ]
        if not values:
            return None
        return statistics.median(values)
