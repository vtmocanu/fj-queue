"""M7 performance smoke (PRD #61 line 178).

Synthetic large-instance fixture: ~2000 jobs / 50 repos / 30 runners.
Asserts each pure stage (aggregate, render_plain, render_rich, JSON
serialize) completes within a stated budget. Budgets are intentionally
generous so that ordinary CI/laptop variance doesn't flake the suite;
they still catch the kind of regression that matters (O(n^2) creep,
accidental per-job network or filesystem touches, exponential rich
layout cost).

Worst-case branch: a separate test feeds N distinct repo_ids to
`_resolve_repos_for_jobs` and asserts the cache invariant
"one Client.resolve_repo call per unique id" holds at N=2000.

Numbers on a 2023 M-series MacBook (single core, untuned Python 3.14):

    aggregate(2000 jobs, 50 repos, 30 runners)  ~25-40 ms
    render_plain(snapshot)                       ~15-25 ms
    render_rich(snapshot) -> Console.print       ~150-250 ms
    render_json(snapshot)                        ~10-20 ms

Budgets below carry a ~10x headroom for slower CI hardware.
"""

from __future__ import annotations

import io
import json
import time
from datetime import datetime, timezone

import pytest

import fj_queue as fq


UTC = timezone.utc
T0 = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
HOST = "git.example.com"

# Performance budgets, in seconds. Each is the wall-clock cap for ONE
# invocation of the named stage at the 2000-job / 50-repo scale.
# A budget tripped on real-life slow CI is fine to bump; a budget
# tripped on a developer laptop is a real regression to investigate.
BUDGET_AGGREGATE_S = 0.5
BUDGET_RENDER_PLAIN_S = 1.0
BUDGET_RENDER_RICH_S = 3.0
BUDGET_RENDER_JSON_S = 1.0


# ---------------------------------------------------------------------------
# Synthetic fixture
# ---------------------------------------------------------------------------


def _big_fixture(n_jobs: int = 2000, n_repos: int = 50, n_runners: int = 30):
    """Build a deterministic ~2000-job / 50-repo / 30-runner workload.

    Repos are addressed by integer id 1..n_repos; the resolved slug
    map gives each a stable `owner/repo-NN` name. Jobs round-robin
    across repos. Labels cycle through three common shapes:
    `(grunt,)`, `(grunt, docker)`, `(gpu,)`. Half the jobs are
    `waiting`, the other half are `running` (with a real task_id so
    they don't end up in the queue).
    """
    runners = []
    for rid in range(1, n_runners + 1):
        # 20 active, 10 offline; labels cycle so superset matching has work.
        status = "active" if rid % 3 != 0 else "offline"
        if rid % 4 == 0:
            labels = ("grunt", "docker")
        elif rid % 4 == 1:
            labels = ("grunt",)
        else:
            labels = ("grunt", "docker", "gpu")
        runners.append(
            fq.Runner(
                id=rid,
                name=f"runner-{rid}",
                status=status,
                version="v12.7.3",
                labels=labels,
                ephemeral=False,
            )
        )

    jobs = []
    for jid in range(1, n_jobs + 1):
        repo_id = ((jid - 1) % n_repos) + 1
        status = "waiting" if jid % 2 == 0 else "running"
        task_id = 0 if status == "waiting" else 100000 + jid
        if jid % 5 == 0:
            runs_on = ("grunt", "docker")
        elif jid % 5 == 1:
            runs_on = ("grunt",)
        elif jid % 5 == 2:
            runs_on = ("gpu",)
        elif jid % 5 == 3:
            runs_on = ()
        else:
            runs_on = ("grunt", "docker", "gpu")  # superset case
        jobs.append(
            fq.RawJob(
                id=jid,
                name=f"job-{jid}",
                status=status,
                repo_id=repo_id,
                owner_id=1,
                runs_on=runs_on,
                needs=() if jid % 7 else ("setup",),
                task_id=task_id,
                attempt=1,
                handle=f"h-{jid}",
            )
        )

    repo_names = {rid: f"owner/repo-{rid:02d}" for rid in range(1, n_repos + 1)}
    return runners, jobs, repo_names


@pytest.fixture(scope="module")
def big_snapshot():
    """Module-scoped: aggregate once, reuse across the render tests."""
    runners, jobs, repo_names = _big_fixture()
    return fq.aggregate(
        runners=runners, jobs=jobs, repo_names=repo_names, now=T0, host=HOST
    )


# ---------------------------------------------------------------------------
# Budget assertions
# ---------------------------------------------------------------------------


def test_aggregate_perf_2000_jobs_50_repos():
    """Pure aggregate of the canonical 2000/50/30 workload must
    complete under BUDGET_AGGREGATE_S.
    """
    runners, jobs, repo_names = _big_fixture()
    start = time.perf_counter()
    snap = fq.aggregate(
        runners=runners, jobs=jobs, repo_names=repo_names, now=T0, host=HOST
    )
    elapsed = time.perf_counter() - start
    # Sanity: half the jobs are running (task_id != 0), half waiting.
    assert snap.totals.running == 1000
    assert snap.totals.waiting == 1000
    assert snap.totals.total == 2000
    assert len(snap.queue) == 1000
    assert len(snap.per_repo) == 50
    assert elapsed < BUDGET_AGGREGATE_S, (
        f"aggregate took {elapsed:.3f}s, budget {BUDGET_AGGREGATE_S}s; "
        f"likely regression to O(n*m) matching or duplicate-repo loop"
    )


def test_render_plain_perf(big_snapshot):
    """`render_plain` returns a string; budget the whole flow."""
    start = time.perf_counter()
    out = fq.render_plain(big_snapshot)
    elapsed = time.perf_counter() - start
    assert "TOTALS:" in out
    assert elapsed < BUDGET_RENDER_PLAIN_S, (
        f"render_plain took {elapsed:.3f}s, budget {BUDGET_RENDER_PLAIN_S}s"
    )


def test_render_rich_perf(big_snapshot):
    """`render_rich` returns a Renderable; print it to a StringIO at
    width=120 with force_terminal=False so the Console render path
    (the work that happens on a real terminal) is included.
    """
    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, no_color=True)
    start = time.perf_counter()
    console.print(fq.render_rich(big_snapshot))
    elapsed = time.perf_counter() - start
    assert "Queue" in buf.getvalue() or "queue" in buf.getvalue()
    assert elapsed < BUDGET_RENDER_RICH_S, (
        f"render_rich took {elapsed:.3f}s, budget {BUDGET_RENDER_RICH_S}s"
    )


def test_render_json_perf(big_snapshot):
    """JSON serialize (`render_json`) must stay snappy: this is the
    hot path for AI-agent consumers (--format json).
    """
    start = time.perf_counter()
    out = fq.render_json(big_snapshot)
    elapsed = time.perf_counter() - start
    parsed = json.loads(out)
    assert parsed["totals"]["total"] == 2000
    assert len(parsed["queue"]) == 1000
    assert elapsed < BUDGET_RENDER_JSON_S, (
        f"render_json took {elapsed:.3f}s, budget {BUDGET_RENDER_JSON_S}s"
    )


# ---------------------------------------------------------------------------
# Worst-case cache invariant (PRD line 178: "worst-case many-distinct
# repos exercises the cache + optional concurrent cold-resolution").
# ---------------------------------------------------------------------------


def test_resolve_repos_for_jobs_at_scale_calls_resolver_once_per_unique_id():
    """At N=2000 jobs spread across 50 unique repo_ids, the helper
    must call `resolve_repo` exactly 50 times (once per unique id),
    NOT 2000 times. This pins the cache contract under load: the
    aggregate hot path cannot accidentally drop the dedupe.
    """

    class _CountingClient:
        def __init__(self):
            self.calls: dict[int, int] = {}

        def resolve_repo(self, rid: int) -> str:
            self.calls[rid] = self.calls.get(rid, 0) + 1
            return f"owner/repo-{rid:02d}"

    _, jobs, _ = _big_fixture(n_jobs=2000, n_repos=50)
    client = _CountingClient()
    out = fq._resolve_repos_for_jobs(client, jobs)
    assert len(out) == 50
    assert sum(client.calls.values()) == 50
    assert max(client.calls.values()) == 1


def test_resolve_repos_for_jobs_worst_case_all_distinct_repo_ids():
    """Worst case: every job has a UNIQUE repo_id (no cache hits).
    The helper still completes within a reasonable budget and still
    calls the resolver exactly once per id. This is the cold-start
    branch where a future concurrent-resolution optimization would
    show up.
    """

    class _CountingClient:
        def __init__(self):
            self.calls = 0

        def resolve_repo(self, rid: int) -> str:
            self.calls += 1
            return f"owner/repo-{rid}"

    jobs = [
        fq.RawJob(
            id=jid, name=f"job-{jid}", status="waiting",
            repo_id=jid,                   # every job a unique repo
            owner_id=1, runs_on=("grunt",), needs=(),
            task_id=0, attempt=1, handle=f"h-{jid}",
        )
        for jid in range(1, 2001)
    ]
    client = _CountingClient()
    start = time.perf_counter()
    out = fq._resolve_repos_for_jobs(client, jobs)
    elapsed = time.perf_counter() - start
    assert len(out) == 2000
    assert client.calls == 2000
    assert elapsed < BUDGET_AGGREGATE_S, (
        f"_resolve_repos_for_jobs (cold) took {elapsed:.3f}s, "
        f"budget {BUDGET_AGGREGATE_S}s"
    )
