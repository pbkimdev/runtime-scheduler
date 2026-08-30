# The ledger

`ledger/YYYY-MM.json` is the scheduler's own record of consumed compute for one
calendar month. It is the primary quota source (ADR 0008); the vendor billing
reports are the reconciliation baseline, not the running total.

The allocate workflow rewrites and commits the file when the tick was worth
recording: new jobs arrived, a reconciliation ran, or the cursor has moved more
than `commit_heartbeat_minutes` past the committed one. A quiet tick leaves the
file alone, so a quiet organization gets an hourly commit rather than 96 a day.
The heartbeat is also what keeps the scheduled workflows alive, because GitHub
disables them after 60 days without repository activity (evidence #8).

## Format

One JSON object. The header keys are sorted; each job is on its own line so a
commit diff shows the jobs that arrived and nothing else.

| Key | Meaning |
|---|---|
| `schema` | Format version. 1 today |
| `month` | `YYYY-MM`, matching the file name |
| `cursor` | The incremental read position in the jobs API. The next tick scans from `cursor - overlap_minutes` |
| `reconciled_on` | `YYYY-MM-DD` of the last reconciliation, so a re-run of the same tick does not reconcile twice |
| `drift_alert_until` | While the tick is before this, every provider's margin doubles. Set for 24 hours when a reconciliation finds drift past the threshold |
| `corrections` | Ledger snaps applied after a drift beyond the threshold. Each carries a signed `delta` in native units |
| `reconciliations` | Every comparison against a vendor report, kept for the audit trail |
| `jobs` | One record per finished job |

## A job record

| Field | Meaning |
|---|---|
| `key` | `run_id:run_attempt:job_id`. A re-run attempt is a new allocation (E15) |
| `repo`, `private` | Where the job ran and whether GitHub bills it |
| `name` | The job name. A routed job's name ends with `/ primary`, `/ fallback` or `/ last` |
| `labels` | The `runs-on` labels as written in the workflow |
| `provider` | `blacksmith-` prefix, `archbox-` prefix, else `github` |
| `family` | `windows` or `macos` in a label, else `linux` |
| `routed` | True only for a routed job in a private repository that is not a fork pull request. Only routed jobs count in a family's allocation denominator |
| `event` | The triggering event |
| `created_at`, `started_at`, `completed_at` | From the jobs payload. `started_at - created_at` is the queue-latency proxy |
| `conclusion` | GitHub's own verdict for the job |
| `work` | The `work` step's conclusion, or `none` when the job has no such step |
| `infra` | True when the job failed while its work step never reported success or failure (ADR 0007) |
| `native` | Native units: what this cost the provider's allowance. Public GitHub jobs are 0; Archbox is 0 |
| `normalized` | Normalized units: one minute on a 2-vCPU x64 Linux runner. Used only for allocation shares |
| `latency_s` | `started_at - created_at` in seconds |

## What must never be here

Job identifiers and minutes only. This repository is public. No secrets, no
repository content, no log text.
