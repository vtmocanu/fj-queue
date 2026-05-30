# Caveats

Three caveats are load-bearing for correct interpretation of the output.

## FIFO approximation

The `queue` array is sorted by job `id` ascending. The real Forgejo dispatcher
orders available jobs by `(updated, id)` ASC and considers only jobs with
`status = waiting AND task_id = 0`. So the displayed queue order is a
**documented approximation** of "roughly who's next," not the literal dispatch
order.

Good for human triage and for an agent asking "is my repo near the front?".
Not a contract on which exact job the next freed runner will pick up.

## Blocked jobs are invisible

The `/api/v1/admin/actions/runners/jobs` endpoint returns only jobs in
`waiting` or `running` status. Jobs in `blocked` state (`needs` dependencies
unmet, no task assigned) are **not returned at all**.

fj-queue documents this gap rather than papering over it: a job that
disappears from the dashboard may have moved to `blocked`, not finished. Use
the per-repo web UI to see truly blocked jobs.

## Filter semantics

`--repo` and `--label` narrow **only** `Snapshot.queue`. The following
fields are **always instance-wide**, regardless of any filter:

- `totals`
- `per_repo`
- `runners`
- `warnings`
- `schedulable_labels`

Each surviving entry in a filtered `queue` keeps its **global** `position`.
Gaps in position numbers are intentional, not a bug.

This ensures the agent journey, "read global saturation from `totals`, then
find my repo's backlog in `per_repo` / `queue`," works correctly under any
filter. If the filter also scoped totals, an agent could not distinguish
instance-wide saturation from just its own repo being backed up.

`--label` is also distinct from the server-side `?labels=` filter on the
Forgejo API: the server filter means "jobs whose `runs_on` includes at least
these labels," which is not the same as "jobs a runner with these labels can
run." fj-queue applies `--label` client-side, after aggregation, with the
subset-filter semantics described above.
