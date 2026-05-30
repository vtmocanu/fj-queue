"""M2 aggregation tests.

PRD #61 §M2 + §Scheduling semantics. Each test is named after the rule
or edge case from the PRD's Test Plan ("Aggregation units") so a failure
points straight at the broken correctness anchor.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fj_queue as fq


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 27, 14, 3, 11, tzinfo=timezone.utc)
HOST = "git.example.com"


def _runner(rid: int, *, status: str = "active", labels=("grunt",), name=None):
    return fq.Runner(
        id=rid,
        name=name if name is not None else f"runner-{rid}",
        status=status,
        version="v12.7.3",
        labels=tuple(labels),
        ephemeral=False,
    )


def _job(
    jid: int,
    *,
    status: str = "waiting",
    repo_id: int = 100,
    owner_id: int = 1,
    runs_on=("grunt",),
    needs=(),
    task_id: int = 0,
    attempt: int = 1,
    name: str | None = None,
):
    return fq.RawJob(
        id=jid,
        name=name or f"job-{jid}",
        status=status,
        repo_id=repo_id,
        owner_id=owner_id,
        runs_on=tuple(runs_on),
        needs=tuple(needs),
        task_id=task_id if status == "running" else 0,
        attempt=attempt,
        handle=f"h-{jid}",
    )


def _agg(runners=(), jobs=(), repo_names=None, **kwargs):
    return fq.aggregate(
        runners=runners,
        jobs=jobs,
        repo_names=repo_names or {},
        now=NOW,
        host=HOST,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_inputs_produce_valid_snapshot():
    """PRD success criterion: empty queue is exit 0, not error."""
    snap = _agg()
    assert snap.host == HOST
    assert snap.schema_version == 1
    assert snap.as_of == NOW
    assert snap.runners == ()
    assert snap.totals == fq.Totals(0, 0, 0)
    assert snap.per_repo == ()
    assert snap.queue == ()
    assert snap.schedulable_labels == ()
    assert snap.warnings == ()


def test_naive_datetime_rejected():
    """A naive (no-tzinfo) `now` would emit a non-RFC3339 string in M3.
    Fail loud at the boundary, not in a downstream snapshot diff.
    """
    naive = datetime(2026, 5, 27, 14, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        fq.aggregate(
            runners=(),
            jobs=(),
            repo_names={},
            now=naive,
            host=HOST,
        )


def test_filter_fields_echoed_into_snapshot():
    snap = _agg(filter_repo="owner-c/theme-api", filter_label=("grunt",))
    assert snap.filter_repo == "owner-c/theme-api"
    assert snap.filter_label == ("grunt",)


# ---------------------------------------------------------------------------
# Post-aggregate filter scoping (`_scope_snapshot_queue`).
#
# Per PRD §JSON contract line 94 and §Progress checkpoint M6 line 159:
# `--repo` / `--label` scope `Snapshot.queue` ONLY. totals, per_repo,
# runners, warnings, and schedulable_labels stay instance-wide; surviving
# queue entries keep their GLOBAL `position`.
# ---------------------------------------------------------------------------


def _make_multi_repo_snapshot():
    """Build a snapshot with 4 waiting jobs across 2 repos, 1 running
    job in a third repo, and 2 runners with mixed labels. Used as the
    fixture for the post-aggregate filter tests below.
    """
    runners = [
        _runner(1, status="active", labels=("grunt", "docker")),
        _runner(2, status="offline", labels=("special",)),
    ]
    jobs = [
        _job(10, repo_id=85, runs_on=("grunt",)),                 # pos 1
        _job(20, repo_id=99, runs_on=("special",)),               # pos 2
        _job(30, repo_id=85, runs_on=("grunt", "docker")),        # pos 3
        _job(40, repo_id=99, runs_on=("grunt",)),                 # pos 4
        _job(50, status="running", repo_id=42, runs_on=("grunt",)),
    ]
    repo_names = {85: "alpha/repo", 99: "beta/repo", 42: "running/repo"}
    return _agg(runners=runners, jobs=jobs, repo_names=repo_names)


def test_scope_returns_input_when_no_filter():
    snap = _make_multi_repo_snapshot()
    scoped = fq._scope_snapshot_queue(snap, filter_repo=None, filter_label=())
    assert scoped is snap  # identity: no-op fast path


def test_scope_repo_keeps_only_matching_queue_entries():
    snap = _make_multi_repo_snapshot()
    scoped = fq._scope_snapshot_queue(
        snap, filter_repo="alpha/repo", filter_label=()
    )
    assert tuple(j.job_id for j in scoped.queue) == (10, 30)
    # Other top-level fields untouched.
    assert scoped.totals == snap.totals
    assert scoped.per_repo == snap.per_repo
    assert scoped.runners == snap.runners
    assert scoped.warnings == snap.warnings
    assert scoped.schedulable_labels == snap.schedulable_labels


def test_scope_preserves_global_positions_with_gaps():
    """Surviving queue entries keep their 1-based GLOBAL position over
    the unfiltered waiting set. Gaps are intentional.
    """
    snap = _make_multi_repo_snapshot()
    scoped = fq._scope_snapshot_queue(
        snap, filter_repo="alpha/repo", filter_label=()
    )
    positions = [j.position for j in scoped.queue]
    assert positions == [1, 3]  # NOT [1, 2]


def test_scope_label_subset_semantics():
    """--label uses subset semantics: a job matches iff its runs_on is
    a superset of the requested labels.
    """
    snap = _make_multi_repo_snapshot()
    # `grunt` alone keeps jobs 10, 30, 40 (positions 1, 3, 4).
    scoped = fq._scope_snapshot_queue(
        snap, filter_repo=None, filter_label=("grunt",)
    )
    assert tuple(j.job_id for j in scoped.queue) == (10, 30, 40)
    assert [j.position for j in scoped.queue] == [1, 3, 4]

    # `grunt` + `docker` keeps only job 30.
    scoped2 = fq._scope_snapshot_queue(
        snap, filter_repo=None, filter_label=("grunt", "docker")
    )
    assert tuple(j.job_id for j in scoped2.queue) == (30,)
    assert scoped2.queue[0].position == 3


def test_scope_combined_repo_and_label():
    snap = _make_multi_repo_snapshot()
    scoped = fq._scope_snapshot_queue(
        snap, filter_repo="alpha/repo", filter_label=("docker",)
    )
    # Only job 30 is in alpha/repo AND has docker in runs_on.
    assert tuple(j.job_id for j in scoped.queue) == (30,)
    assert scoped.queue[0].position == 3


def test_scope_warnings_remain_instance_wide():
    """An unschedulable job in a non-filtered repo still appears in
    warnings when a filter for a different repo is active.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(100, repo_id=85, runs_on=("grunt",)),
        _job(200, repo_id=99, runs_on=("gpu",)),  # no runner has gpu
    ]
    repo_names = {85: "alpha/repo", 99: "stuck/repo"}
    snap = _agg(runners=runners, jobs=jobs, repo_names=repo_names)
    # Pre-condition: warning exists for stuck/repo.
    assert any(w.repo == "stuck/repo" for w in snap.warnings)

    scoped = fq._scope_snapshot_queue(
        snap, filter_repo="alpha/repo", filter_label=()
    )
    # queue is alpha/repo only
    assert {j.repo for j in scoped.queue} == {"alpha/repo"}
    # warning for stuck/repo still surfaces
    assert any(w.repo == "stuck/repo" for w in scoped.warnings)


# ---------------------------------------------------------------------------
# Online detection: status in {active, idle}; idle is NOT offline.
# ---------------------------------------------------------------------------


def test_is_online_active():
    assert fq.is_online(_runner(1, status="active")) is True


def test_is_online_idle_is_treated_as_online():
    """PRD: 'idle = registered-and-polling-but-free, fully available'.
    The PRD explicitly warns: checking only `active` would flag every
    job 'stuck' whenever runners are merely idle (the normal quiet
    state).
    """
    assert fq.is_online(_runner(1, status="idle")) is True


def test_is_online_offline():
    assert fq.is_online(_runner(1, status="offline")) is False


def test_is_online_unknown_status_treated_as_offline():
    """Defensive: swagger does not enum-constrain `status`. Anything
    outside {active, idle, offline} falls through as offline so we
    never crash on a new value and never silently mark it online.
    """
    assert fq.is_online(_runner(1, status="paused")) is False
    assert fq.is_online(_runner(1, status="")) is False


# ---------------------------------------------------------------------------
# Per-runner SUPERSET label matching (the source-verified rule).
# ---------------------------------------------------------------------------


def test_superset_match_runner_labels_satisfy_runs_on():
    """job runs_on=[docker] on runner labels=[docker,grunt]: subset, OK."""
    runners = [_runner(1, status="active", labels=("docker", "grunt"))]
    jobs = [_job(10, runs_on=("docker",), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    job = snap.queue[0]
    assert job.blocked_reason == fq.BLOCKED_WAITING_FOR_RUNNER
    assert snap.warnings == ()


def test_superset_match_runner_missing_label_unschedulable():
    """job needs [gpu], runner only has [docker]: no superset match,
    unschedulable + warning emitted.
    """
    runners = [_runner(1, status="active", labels=("docker",))]
    jobs = [_job(10, runs_on=("gpu",), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_UNSCHEDULABLE
    assert len(snap.warnings) == 1
    w = snap.warnings[0]
    assert w.code == fq.WARN_UNSCHEDULABLE_LABELS
    assert w.job_id == 10
    assert w.runs_on == ("gpu",)


def test_docker_plus_gpu_split_is_unschedulable():
    """THE canonical correctness case. Job needs [docker, gpu]. Two
    runners: one [docker], one [gpu]. The naive 'union of runner labels
    intersects runs_on' rule would flag this as fine (union={docker,gpu}
    covers both). The actual Forgejo dispatcher schedules on a single
    runner whose labels are a superset, and no single runner here is a
    superset of [docker, gpu]. Result: unschedulable.
    """
    runners = [
        _runner(1, status="active", labels=("docker",), name="docker-only"),
        _runner(2, status="active", labels=("gpu",), name="gpu-only"),
    ]
    jobs = [_job(10, runs_on=("docker", "gpu"), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_UNSCHEDULABLE
    assert len(snap.warnings) == 1
    assert snap.warnings[0].job_id == 10


def test_docker_plus_gpu_satisfied_by_a_supersetrunner():
    """Same job runs_on=[docker, gpu], but now one runner is
    [docker, gpu, extra]. Superset: schedulable -> waiting_for_runner.
    """
    runners = [
        _runner(1, status="active", labels=("docker",), name="docker-only"),
        _runner(
            2,
            status="active",
            labels=("docker", "gpu", "extra"),
            name="big",
        ),
    ]
    jobs = [_job(10, runs_on=("docker", "gpu"), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_WAITING_FOR_RUNNER
    assert snap.warnings == ()


def test_offline_runner_does_not_count_toward_schedulability():
    """If the only runner with the right labels is offline, the job is
    unschedulable.
    """
    runners = [
        _runner(1, status="offline", labels=("docker", "gpu")),
    ]
    jobs = [_job(10, runs_on=("docker", "gpu"), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_UNSCHEDULABLE


def test_idle_runner_DOES_count_toward_schedulability():
    """idle is online (PRD: 'idle = registered-and-polling-but-free')."""
    runners = [
        _runner(1, status="idle", labels=("docker", "gpu")),
    ]
    jobs = [_job(10, runs_on=("docker", "gpu"), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_WAITING_FOR_RUNNER
    assert snap.warnings == ()


# ---------------------------------------------------------------------------
# All-runners-offline.
# ---------------------------------------------------------------------------


def test_all_runners_offline_every_waiting_job_unschedulable():
    """PRD success criterion. No online runner exists, so every waiting
    job with empty needs becomes unschedulable. Jobs with non-empty
    needs stay blocked_on_needs (NOT unschedulable, per PRD).
    """
    runners = [
        _runner(1, status="offline", labels=("docker",)),
        _runner(2, status="offline", labels=("grunt",)),
    ]
    jobs = [
        _job(10, runs_on=("docker",), needs=()),
        _job(11, runs_on=("grunt",), needs=()),
        _job(12, runs_on=("grunt",), needs=("upstream",)),
    ]
    snap = _agg(runners=runners, jobs=jobs)
    by_id = {j.job_id: j for j in snap.queue}
    assert by_id[10].blocked_reason == fq.BLOCKED_UNSCHEDULABLE
    assert by_id[11].blocked_reason == fq.BLOCKED_UNSCHEDULABLE
    assert by_id[12].blocked_reason == fq.BLOCKED_ON_NEEDS
    warned = {w.job_id for w in snap.warnings}
    # Jobs 10 + 11 warn; job 12 is needs-gated and excluded.
    assert warned == {10, 11}


# ---------------------------------------------------------------------------
# needs-gated jobs are EXCLUDED from unschedulable warnings.
# ---------------------------------------------------------------------------


def test_needs_gated_job_is_not_flagged_unschedulable_even_if_no_runner_matches():
    """PRD risk mitigation: a job with non-empty needs is blocked_on_needs,
    NEVER unschedulable. We cannot tell whether the deps will eventually
    satisfy and a matching runner exists or appears.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        # runs_on does not match any runner, BUT needs is non-empty:
        # must NOT be flagged unschedulable.
        _job(10, runs_on=("nonexistent",), needs=("upstream",)),
    ]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_ON_NEEDS
    assert snap.warnings == ()


# ---------------------------------------------------------------------------
# Empty runs_on (the contested edge case; documented design choice).
# ---------------------------------------------------------------------------


def test_empty_runs_on_with_online_runner_is_waiting_for_runner():
    """Empty set is a subset of every label set, so under the formal
    PRD scheduling rule any online runner satisfies it. Documented
    deviation from the PRD JSON-contract EXAMPLE (which shows job 79969
    with runs_on=[] as unschedulable_labels): the [docker]+[gpu]
    example in the same PRD section is what fixes the formal rule, and
    it implies empty runs_on is schedulable when any online runner
    exists. We surface this design decision in code review.
    """
    runners = [_runner(1, status="active", labels=("docker",))]
    jobs = [_job(10, runs_on=(), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_WAITING_FOR_RUNNER


def test_empty_runs_on_with_no_online_runner_is_unschedulable():
    """All-offline still flags empty-runs_on as unschedulable, because
    no online runner exists to satisfy anything.
    """
    runners = [_runner(1, status="offline", labels=("docker",))]
    jobs = [_job(10, runs_on=(), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_UNSCHEDULABLE


# ---------------------------------------------------------------------------
# FIFO re-sort.
# ---------------------------------------------------------------------------


def test_queue_resorted_id_ascending_from_id_desc_input():
    """API serves id DESC; aggregate re-sorts to id ASC. position is
    1-based across all waiting jobs (incl. needs-gated).
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs_desc = [
        _job(30, runs_on=("grunt",)),
        _job(20, runs_on=("grunt",)),
        _job(10, runs_on=("grunt",)),
    ]
    snap = _agg(runners=runners, jobs=jobs_desc)
    assert [j.job_id for j in snap.queue] == [10, 20, 30]
    assert [j.position for j in snap.queue] == [1, 2, 3]


def test_position_spans_needs_gated_jobs_too():
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(10, runs_on=("grunt",), needs=("dep",)),  # blocked_on_needs
        _job(20, runs_on=("grunt",)),                  # waiting_for_runner
        _job(30, runs_on=("nope",)),                   # unschedulable
    ]
    snap = _agg(runners=runners, jobs=jobs)
    positions = {j.job_id: j.position for j in snap.queue}
    assert positions == {10: 1, 20: 2, 30: 3}


def test_running_job_has_no_position_and_no_blocked_reason():
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(10, status="running", task_id=42, runs_on=("grunt",)),
        _job(20, status="waiting", runs_on=("grunt",)),
    ]
    snap = _agg(runners=runners, jobs=jobs)
    # queue is waiting-only.
    assert [j.job_id for j in snap.queue] == [20]
    assert snap.queue[0].position == 1
    # Running goes into totals + per_repo, not queue.
    assert snap.totals == fq.Totals(running=1, waiting=1, total=2)


# ---------------------------------------------------------------------------
# Totals + per_repo + ordering contracts.
# ---------------------------------------------------------------------------


def test_per_repo_sorted_by_slug_and_counted_correctly():
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(10, repo_id=2, status="waiting"),
        _job(11, repo_id=2, status="waiting"),
        _job(12, repo_id=2, status="running", task_id=99),
        _job(20, repo_id=1, status="waiting"),
        _job(21, repo_id=1, status="running", task_id=100),
    ]
    repo_names = {1: "alpha/repo", 2: "beta/repo"}
    snap = _agg(runners=runners, jobs=jobs, repo_names=repo_names)
    assert [pr.repo for pr in snap.per_repo] == ["alpha/repo", "beta/repo"]
    alpha = snap.per_repo[0]
    beta = snap.per_repo[1]
    assert (alpha.waiting, alpha.running, alpha.total) == (1, 1, 2)
    assert (beta.waiting, beta.running, beta.total) == (2, 1, 3)


def test_duplicate_repo_id_resolved_once_no_duplicate_rows():
    """PRD Test Plan: duplicate repo_id (resolved once, exposed once)."""
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(10, repo_id=42, status="waiting"),
        _job(11, repo_id=42, status="waiting"),
        _job(12, repo_id=42, status="running", task_id=99),
    ]
    snap = _agg(runners=runners, jobs=jobs, repo_names={42: "foo/bar"})
    assert len(snap.per_repo) == 1
    row = snap.per_repo[0]
    assert row.repo == "foo/bar"
    assert (row.waiting, row.running, row.total) == (2, 1, 3)


def test_unresolved_repo_id_falls_back_to_repo_hash_id():
    """PRD: `repo_id 404 -> repo#<id> fallback without aborting`. Here
    the caller did not include repo_id=7 in repo_names; aggregate must
    still emit a row labeled `repo#7`.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [_job(10, repo_id=7, status="waiting")]
    snap = _agg(runners=runners, jobs=jobs, repo_names={})
    assert snap.per_repo[0].repo == "repo#7"
    assert snap.queue[0].repo == "repo#7"


def test_runners_sorted_by_name():
    """PRD ordering contract."""
    runners = [
        _runner(3, name="zeta", status="active"),
        _runner(1, name="alpha", status="active"),
        _runner(2, name="mike", status="active"),
    ]
    snap = _agg(runners=runners)
    assert [r.name for r in snap.runners] == ["alpha", "mike", "zeta"]


def test_schedulable_labels_is_union_of_online_runners_only_sorted():
    runners = [
        _runner(1, status="active", labels=("docker", "grunt")),
        _runner(2, status="idle", labels=("gpu",)),
        _runner(3, status="offline", labels=("never-shown",)),
    ]
    snap = _agg(runners=runners)
    assert snap.schedulable_labels == ("docker", "gpu", "grunt")


def test_warnings_sorted_by_job_id():
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(30, runs_on=("nope",)),
        _job(10, runs_on=("nope",)),
        _job(20, runs_on=("nope",)),
    ]
    snap = _agg(runners=runners, jobs=jobs)
    assert [w.job_id for w in snap.warnings] == [10, 20, 30]


# ---------------------------------------------------------------------------
# Live fixture smoke test (the snapshot the wider test suite snapshots in M7).
# ---------------------------------------------------------------------------


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture_runners() -> list[fq.Runner]:
    data = json.loads((FIXTURES / "runners-live.json").read_text())
    # Reuse the M1 normalizer so this exercises the same parsing path.
    return [fq._normalize_runner(d) for d in data]


def _load_fixture_jobs() -> list[fq.RawJob]:
    data = json.loads((FIXTURES / "jobs-live.json").read_text())
    return [fq._normalize_job(d) for d in data]


# ---------------------------------------------------------------------------
# Brief-required cases (4): C1 exact, FIFO mixed waiting+running,
# task_id preservation across the waiting/running boundary, and
# clock-injection round-trip.
# ---------------------------------------------------------------------------


def test_c1_brief_exact_runner_docker_grunt_job_grunt():
    """Brief C1 (literal): 1 runner [docker, grunt], 1 job runs_on=[grunt].
    Expected: schedulable, blocked_reason=waiting_for_runner, NO warning.
    """
    runners = [_runner(1, status="active", labels=("docker", "grunt"))]
    jobs = [_job(10, runs_on=("grunt",), needs=())]
    snap = _agg(runners=runners, jobs=jobs)
    assert snap.queue[0].blocked_reason == fq.BLOCKED_WAITING_FOR_RUNNER
    assert snap.warnings == ()


def test_fifo_mixed_waiting_and_running_queue_is_waiting_only_sorted_asc():
    """Brief test plan: feed jobs `id=[80239, 80237, 80238]` (out of order,
    mixed waiting + running) -> queue returned id ASC over WAITING ONLY.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(80239, status="waiting", runs_on=("grunt",)),
        _job(80237, status="running", task_id=99, runs_on=("grunt",)),
        _job(80238, status="waiting", runs_on=("grunt",)),
    ]
    snap = _agg(runners=runners, jobs=jobs)
    # queue is waiting-only, id ASC.
    assert [j.job_id for j in snap.queue] == [80238, 80239]
    assert [j.position for j in snap.queue] == [1, 2]
    # Totals reflect all 3 jobs (instance-wide).
    assert snap.totals == fq.Totals(running=1, waiting=2, total=3)


def test_task_id_preserved_None_when_waiting_int_when_running():
    """Forgejo emits task_id=0 while waiting and a real id once running.
    M2 normalizes the waiting-sentinel to None on the Job model so the
    type carries the meaning. Brief: 'task_id == 0 while waiting,
    != 0 while running is preserved through M2.'
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(10, status="waiting", runs_on=("grunt",)),         # task_id=0 on wire
        _job(20, status="running", task_id=98765, runs_on=("grunt",)),
    ]
    snap = _agg(runners=runners, jobs=jobs)
    # Only the waiting job lands in queue; verify task_id normalized.
    assert len(snap.queue) == 1
    assert snap.queue[0].job_id == 10
    assert snap.queue[0].task_id is None  # was 0 on the wire
    # The running job is not in queue, but per_repo still counts it.
    pr = snap.per_repo[0]
    assert (pr.running, pr.waiting) == (1, 1)


def test_clock_injection_as_of_round_trips_exactly():
    """Brief: aggregate(..., now=<frozen>) -> Snapshot.as_of equals it
    byte-for-byte. The clock MUST be the injected datetime, never read
    from time.time() / datetime.now() / etc.
    """
    frozen = datetime(2026, 5, 27, 18, 30, 0, tzinfo=timezone.utc)
    snap = fq.aggregate(
        runners=(),
        jobs=(),
        repo_names={},
        now=frozen,
        host=HOST,
    )
    assert snap.as_of == frozen
    # Identity-preserving (same UTC offset, same microsecond).
    assert snap.as_of.tzinfo is timezone.utc


def test_live_fixture_aggregates_consistently():
    """Smoke against the live-captured fixtures from M1. Confirms the
    two waiting jobs (both with non-empty needs in the live capture)
    are blocked_on_needs and NOT emit warnings.
    """
    runners = _load_fixture_runners()
    jobs = _load_fixture_jobs()
    # Both live jobs reference repos that resolve via M1 client; for the
    # pure aggregate test we provide a stable map.
    repo_names = {589: "owner-a/repo-a", 586: "owner-b/harbor"}
    snap = _agg(runners=runners, jobs=jobs, repo_names=repo_names)

    # Live capture: 1 running + 1 waiting.
    assert snap.totals.running == 1
    assert snap.totals.waiting == 1
    assert snap.totals.total == 2

    # Online runner set: just runner id=3 (status=active). Labels
    # contribute to schedulable_labels.
    assert snap.schedulable_labels == ("docker", "grunt")

    # The waiting job (id=79969) has 7 needs -> blocked_on_needs, NOT a
    # warning, even though its runs_on=[] (which would otherwise be
    # debated).
    waiting = [j for j in snap.queue if j.job_id == 79969]
    assert len(waiting) == 1
    assert waiting[0].blocked_reason == fq.BLOCKED_ON_NEEDS
    # No `unschedulable_labels` warnings should fire on the live
    # snapshot.
    assert snap.warnings == ()

    # The waiting job is in `owner-b/harbor` per the repo_names map.
    assert waiting[0].repo == "owner-b/harbor"



# ---------------------------------------------------------------------------
# Runner metrics pass-through (aggregate stays pure: it just carries the
# pre-fetched runner_pods + metrics_error onto the Snapshot; the actual
# Prometheus I/O lives in _do_one_fetch).
# ---------------------------------------------------------------------------


def _pod(name, *, node="node-a-1", cpu=0.01, mem=700_000_000):
    return fq.PodResource(pod=name, node=node, cpu_cores=cpu, memory_bytes=mem)


def test_runner_pods_pass_through_unchanged():
    pods = (
        _pod("forgejo-runner-abc", node="node-a-1", cpu=0.001, mem=763322368),
        _pod("forgejo-runner-def", node="node-a-2", cpu=0.01, mem=711917568),
    )
    snap = _agg(runner_pods=pods)
    assert snap.runner_pods == pods
    assert snap.metrics_error is None


def test_metrics_error_passes_through_with_empty_pods():
    snap = _agg(runner_pods=(), metrics_error="timeout querying prometheus")
    assert snap.runner_pods == ()
    assert snap.metrics_error == "timeout querying prometheus"


def test_runner_pods_default_to_empty_when_omitted():
    """Existing callers that don't pass metrics get a stable empty default
    (no error), so the JSON/render contract holds without them.
    """
    snap = _agg()
    assert snap.runner_pods == ()
    assert snap.metrics_error is None


def test_aggregate_does_not_fetch_metrics_itself(monkeypatch):
    """Purity guard: aggregate() must NOT reach for the network. If it
    ever called fetch_runner_pods OR fetch_ncps_status, this blows up.
    """
    def _boom(*a, **k):
        raise AssertionError("aggregate() reached for the network")

    monkeypatch.setattr(fq, "fetch_runner_pods", _boom)
    monkeypatch.setattr(fq, "fetch_ncps_status", _boom)
    snap = _agg(
        runners=[_runner(1, status="active")],
        jobs=[_job(10, runs_on=("grunt",))],
    )
    # Built fine without any metrics fetch.
    assert snap.runner_pods == ()
    assert snap.ncps is None


# ---------------------------------------------------------------------------
# NCPS status pass-through (same purity contract).
# ---------------------------------------------------------------------------


def test_ncps_passes_through_unchanged():
    ncps = fq.NcpsStatus(
        active=True,
        requests_per_sec=8.5,
        inflight=1,
        upstream_per_sec=0.3,
        bytes_per_sec=4000000.0,
    )
    snap = _agg(ncps=ncps)
    assert snap.ncps is ncps


def test_ncps_defaults_to_none_when_omitted():
    assert _agg().ncps is None
