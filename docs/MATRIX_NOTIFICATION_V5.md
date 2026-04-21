# Matrix notifications v5 (Via)

## Scope

This document describes the v5 notification format for task updates in Via:

- single update flow via journal engine
- canonical task update card (`tpl_task_change`)
- dedup + idempotent Matrix send (`txn_id`)
- retry policy for Matrix API

Legacy update/status notification path is not used in runtime.

## Required settings

- `POLLING_INTERVAL_SEC`
- `DEDUP_TTL_HOURS`
- `SUBJECT_MAX_LEN`

`CHECK_INTERVAL` is kept as alias and may override polling interval if explicitly set.
`PORTAL_BASE_URL` is optional; when empty Via uses `REDMINE_URL` for issue links.

## v5 card format

Rendered by `tpl_task_change`:

- single `<blockquote>` container
- fields on separate lines:
  - project
  - version
  - status
  - priority
  - assignee
- final link: `Открыть задачу`

Plain fallback (`body`) for `issue_updated` / `status_change` is generated in code with `| ` prefix per line.

## Aggregation and dedup

- multiple journals for the same issue in one tick are collapsed into one update
- target fields are collapsed as `first old -> last new`
- dedup key:
  - primary: `issue:{issue_id}:journal:{journal_id}`
  - fallback: `issue:{issue_id}:updated:{updated_on_sec}:event:{event}:h:{change_hash}`

Persistent state uses existing Via database cursor/state tables.

## Matrix idempotency and retry

- Matrix send uses deterministic `txn_id` derived from dedup key
- retry:
  - `5xx`/network: delays `1s`, `3s`, `7s`
  - `429`/`M_LIMIT_EXCEEDED`: uses `retry_after_ms`
  - other `4xx`: no retry

## Redmine contract verification

At poll phase, bot logs contract issues for sampled issues:

- required: `issue.id`, `issue.subject`, `project.name`, `status.name`, `priority.name`
- optional but tracked: `fixed_version.name`, `assigned_to.name`, `updated_on`

Log marker: `journal_contract_check issue_id=...`
