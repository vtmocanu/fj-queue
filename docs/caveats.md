# Caveats

Four caveats are load-bearing for correct interpretation of the output.

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

## Wedged-sentinel detection is heuristic

The `wedged_sentinel` warning flags a waiting job that LOOKS like a
workflow_call expansion sentinel (its `needs` are all namespaced under its own
name, `<name>.<inner>`) in a repo with no other running or queued job. That
shape plus inactivity is consistent with a Forgejo bug (forgejo#12127) where a
rerun-targeted sentinel stays `waiting` forever and holds its concurrency
group, starving later runs. It is a **heuristic**, not a diagnosis:

- **Repo-level approximation.** The admin jobs payload carries no run id, so
  fj-queue cannot ask "is any sibling job of the SAME RUN still active?" It
  approximates with "is any other job of the same REPO running or queued?"
- **Transient false positives.** Right as a run finalizes, there is a short
  window where the sentinel is still `waiting` but every inner job has already
  left the waiting/running set. A single snapshot can flag that as wedged. The
  warning message says so: treat it as real only if it **persists across
  snapshots**.
- **False negatives.** A wrapper job that sets a display `name:` override does
  not match the signature (its wire name no longer prefixes its needs). And a
  healthy concurrent run of the same repo counts as repo activity, suppressing
  the warning for a genuinely wedged sentinel next to it.

If a flagged job persists across snapshots, cancel the wedged run to release
its concurrency group.
