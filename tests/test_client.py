"""M1 client tests: token precedence, pagination boundary, repo lookup fallback.

PRD #61 -- Test Plan, "API client" bullet:
  * token precedence (--token > env > tea config); tea-config path is dropped
    in M1 per the PRD Open Question #2 default, so we only assert
    arg > env > error.
  * runner pagination boundary (page-size multiple and one-over): mock 35
    runners across 2 pages via Link: rel="next" and assert all 35 come back.
  * repo_id 404/403/timeout -> `repo#<id>` fallback without aborting.

The aggregation/JSON/render tests land in later milestones (M2..M7); this
file is the M1 floor.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

import fj_queue as fq


FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Token precedence
# ---------------------------------------------------------------------------


def test_token_arg_beats_env(monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "from-env")
    assert fq.resolve_token(arg_token="from-arg") == "from-arg"


def test_token_env_used_when_no_arg(monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "from-env")
    assert fq.resolve_token(arg_token=None) == "from-env"


def test_token_missing_raises_config_error():
    # Explicit empty env -> no fallback in M1.
    with pytest.raises(fq.ConfigError):
        fq.resolve_token(arg_token=None, env={})


def test_token_empty_arg_falls_through_to_env(monkeypatch):
    # An empty --token (user passed --token "" on the CLI) should not
    # mask the env var. Truthy check, not "is not None".
    monkeypatch.setenv("FORGEJO_TOKEN", "from-env")
    assert fq.resolve_token(arg_token="") == "from-env"


def test_config_error_exit_code():
    assert fq.ConfigError().exit_code == fq.EXIT_USAGE
    assert fq.AuthError().exit_code == fq.EXIT_AUTH
    assert fq.ConnectionError().exit_code == fq.EXIT_CONNECTION
    assert fq.SchemaDrift().exit_code == fq.EXIT_SCHEMA_DRIFT


# ---------------------------------------------------------------------------
# Metrics URL precedence (resolve_metrics_url)
#
# Mirrors the resolve_token precedence tests above, but unlike the token
# there is no error case: a built-in default (prometheus.example.com) always
# exists. Precedence: --metrics-url arg > $FJ_QUEUE_METRICS_URL env > default.
# ---------------------------------------------------------------------------


def test_metrics_url_arg_beats_env():
    assert (
        fq.resolve_metrics_url(arg_url="https://arg.example", env={"FJ_QUEUE_METRICS_URL": "https://env.example"})
        == "https://arg.example"
    )


def test_metrics_url_env_used_when_no_arg():
    assert (
        fq.resolve_metrics_url(arg_url=None, env={"FJ_QUEUE_METRICS_URL": "https://env.example"})
        == "https://env.example"
    )


def test_metrics_url_default_when_no_arg_no_env():
    # Empty env -> the built-in default, no error (contrast resolve_token).
    assert fq.resolve_metrics_url(arg_url=None, env={}) == fq._DEFAULT_METRICS_URL


def test_metrics_url_empty_arg_falls_through_to_env():
    # An empty --metrics-url "" must not mask the env var (truthy check,
    # not "is not None"), matching the resolve_token semantics.
    assert (
        fq.resolve_metrics_url(arg_url="", env={"FJ_QUEUE_METRICS_URL": "https://env.example"})
        == "https://env.example"
    )


def test_metrics_url_empty_env_falls_through_to_default():
    # An empty env value is treated as unset -> default.
    assert fq.resolve_metrics_url(arg_url=None, env={"FJ_QUEUE_METRICS_URL": ""}) == fq._DEFAULT_METRICS_URL


def test_config_repr_masks_token():
    """Config repr / str MUST NOT leak the token. M3 (JSON error envelope),
    M4 (Rich panels), and M5 (watch-mode error display) will all serialize
    Config into user-facing output.
    """
    c = fq.Config(host="git.example.com", token="SECRETXYZ", timeout=5.0)
    assert "SECRETXYZ" not in repr(c)
    assert "SECRETXYZ" not in str(c)
    assert "***" in repr(c)
    assert "git.example.com" in repr(c)  # non-secret fields still visible


def test_config_repr_with_empty_token():
    """Empty token renders as empty masked string, never as 'None' or '***'."""
    c = fq.Config(host="git.example.com", token="", timeout=5.0)
    r = repr(c)
    assert "SECRET" not in r
    # Empty-token marker: masked is '' (not '***'), so the secret-bearing
    # codepath was not taken.
    assert "token=''" in r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner_dict(rid: int, *, status: str = "idle") -> dict:
    return {
        "id": rid,
        "name": f"runner-{rid}",
        "status": status,
        "version": "v12.7.3",
        "labels": ["grunt", "docker"],
        "ephemeral": False,
        # Extra wire-only fields fj_queue ignores; included to make sure
        # we tolerate them without complaint.
        "uuid": f"uuid-{rid}",
        "owner_id": 0,
        "repo_id": 0,
        "description": "",
    }


def _cfg() -> fq.Config:
    return fq.Config(host="git.example.com", token="testtoken", timeout=5.0)


# ---------------------------------------------------------------------------
# Pagination boundary
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_runners_follows_link_header_across_two_pages():
    """35 runners split 30+5 across two pages via Link: rel=\"next\"."""
    page1 = [_runner_dict(i) for i in range(1, 31)]    # 30 runners
    page2 = [_runner_dict(i) for i in range(31, 36)]   # 5 runners
    next_link = (
        '<https://git.example.com/api/v1/admin/actions/runners'
        '?limit=50&page=2>; rel="next", '
        '<https://git.example.com/api/v1/admin/actions/runners'
        '?limit=50&page=2>; rel="last"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "2":
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1, headers={"Link": next_link})

    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(side_effect=handler)

    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()

    assert len(runners) == 35
    assert [r.id for r in runners] == list(range(1, 36))
    # type-normalization sanity
    assert isinstance(runners[0].labels, tuple)
    assert runners[0].labels == ("grunt", "docker")


@respx.mock
def test_fetch_runners_refuses_cross_host_next_link():
    """If `Link: rel="next"` points at a different host, the client MUST NOT
    dispatch (httpx would send the Authorization header to that host
    verbatim). Audit finding M2. Raise SchemaDrift; never follow.
    """
    evil_link = (
        '<https://evil.example.com/api/v1/admin/actions/runners?page=2>; '
        'rel="next"'
    )
    page1 = [_runner_dict(i) for i in range(1, 4)]

    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(200, json=page1, headers={"Link": evil_link}))

    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.SchemaDrift, match="cross-host"):
            c.fetch_runners()


@respx.mock
def test_fetch_runners_refuses_http_downgrade_next_link():
    """Same trust boundary, scheme variant: an http:// next-link on an https
    base would also leak the bearer token. Refuse.
    """
    downgrade_link = (
        '<http://git.example.com/api/v1/admin/actions/runners?page=2>; '
        'rel="next"'
    )
    page1 = [_runner_dict(i) for i in range(1, 4)]

    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(
        return_value=httpx.Response(
            200, json=page1, headers={"Link": downgrade_link}
        )
    )

    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.SchemaDrift, match="non-https"):
            c.fetch_runners()


@pytest.mark.parametrize(
    "next_url,expected_host",
    [
        # Same host, https -> passes through unchanged.
        (
            "https://git.example.com/api/v1/admin/actions/runners?page=2",
            "git.example.com",
        ),
        # Same host with explicit https:// prefix on Config.host.
        (
            "https://git.example.com/api/v1/admin/actions/runners?page=2",
            "https://git.example.com",
        ),
        # Relative URL has no host -> trusted.
        ("/api/v1/admin/actions/runners?page=2", "git.example.com"),
    ],
)
def test_enforce_same_host_accepts_valid(next_url, expected_host):
    assert fq._enforce_same_host(next_url, expected_host) == next_url


@pytest.mark.parametrize(
    "next_url,expected_host,match",
    [
        (
            "https://evil.example.com/api/v1/x",
            "git.example.com",
            "cross-host",
        ),
        (
            "http://git.example.com/api/v1/x",
            "git.example.com",
            "non-https",
        ),
    ],
)
def test_enforce_same_host_rejects(next_url, expected_host, match):
    with pytest.raises(fq.SchemaDrift, match=match):
        fq._enforce_same_host(next_url, expected_host)


@respx.mock
def test_fetch_runners_dedupes_runaway_link_loop():
    """Forgejo v15.0.2 quirk: limit without explicit page=1 returns the full
    set yet still emits rel="next" pointing at page=2 (which then returns the
    tail again). We pass page=1 explicitly AND dedupe by id; verify the
    dedupe arm by mocking a server that double-serves runner id=1.
    """
    overlap = [_runner_dict(1), _runner_dict(2), _runner_dict(3)]
    page2 = [_runner_dict(3)]  # duplicate of last item from page1
    next_link = (
        '<https://git.example.com/api/v1/admin/actions/runners'
        '?limit=50&page=2>; rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=overlap, headers={"Link": next_link})

    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(side_effect=handler)

    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()
    assert [r.id for r in runners] == [1, 2, 3]


@respx.mock
def test_fetch_runners_passes_page_1_explicitly():
    """Defends the Forgejo-quirk fix: the first request MUST include page=1
    so the server honors the limit. We capture the request and assert.
    """
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json=[_runner_dict(1)])

    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(side_effect=handler)

    with fq.Client(_cfg()) as c:
        c.fetch_runners()
    assert captured, "no request captured"
    assert "page=1" in captured[0], f"first URL missing page=1: {captured[0]}"


@respx.mock
def test_fetch_runners_single_page_no_link_header():
    """No Link header -> single fetch, no follow-up."""
    page = [_runner_dict(i) for i in range(1, 4)]  # 3 runners (live shape)
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(200, json=page))

    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()
    assert len(runners) == 3
    assert runners[2].id == 3


# ---------------------------------------------------------------------------
# Jobs endpoint (single unbounded call; bare list)
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_jobs_parses_live_shape():
    body = [
        {
            "id": 83886,
            "attempt": 1,
            "handle": "53e5d92f-1f7e-44dd-9818-d463519d3368",
            "repo_id": 589,
            "owner_id": 15,
            "name": "Chainsaw E2E Tests",
            "needs": ["pipeline.generate", "pipeline.build-push"],
            "runs_on": ["grunt"],
            "task_id": 85492,
            "status": "running",
        },
        {
            "id": 83900,
            "attempt": 1,
            "handle": "abc",
            "repo_id": 85,
            "owner_id": 11,
            "name": "Lint",
            "needs": [],
            "runs_on": ["grunt", "docker"],
            "task_id": 0,
            "status": "waiting",
        },
    ]
    respx.get(
        "https://git.example.com/api/v1/admin/actions/runners/jobs"
    ).mock(return_value=httpx.Response(200, json=body))

    with fq.Client(_cfg()) as c:
        jobs = c.fetch_jobs()
    assert len(jobs) == 2
    assert jobs[0].task_id == 85492
    assert jobs[0].status == "running"
    assert jobs[1].task_id == 0
    assert jobs[1].status == "waiting"
    assert jobs[1].runs_on == ("grunt", "docker")


@respx.mock
def test_fetch_jobs_treats_null_payload_as_empty_list():
    """Forgejo v15.0.2 quirk (verified live against git.example.com on
    2026-05-28 during M6 live acceptance): when the live queue is
    empty, `/api/v1/admin/actions/runners/jobs` returns the bare JSON
    literal `null` rather than `[]`. The client MUST treat that as
    the empty list, not raise SchemaDrift. M6 carry-over per PRD #61
    line 165.
    """
    respx.get(
        "https://git.example.com/api/v1/admin/actions/runners/jobs"
    ).mock(return_value=httpx.Response(200, text="null"))

    with fq.Client(_cfg()) as c:
        jobs = c.fetch_jobs()
    assert jobs == []


@respx.mock
def test_fetch_jobs_null_payload_aggregates_to_empty_snapshot_exit_zero():
    """End-to-end regression for the live-empty-queue case: null body
    + zero runners aggregates to a Snapshot with empty queue and
    `totals.running == totals.waiting == 0`. M3 + M2 both downstream
    of the M1 normalization fix; this asserts the whole pipeline does
    not regress to SchemaDrift on the empty live case.
    """
    respx.get(
        "https://git.example.com/api/v1/admin/actions/runners/jobs"
    ).mock(return_value=httpx.Response(200, text="null"))
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(200, json=[]))

    from datetime import datetime, timezone
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()
        jobs = c.fetch_jobs()
    snap = fq.aggregate(
        runners=runners, jobs=jobs, repo_names={}, now=now, host="git.example.com"
    )
    assert snap.queue == ()
    assert snap.totals.running == 0
    assert snap.totals.waiting == 0
    assert snap.totals.total == 0


@respx.mock
def test_fetch_jobs_tolerates_missing_optional_fields():
    """needs / runs_on absent or null must not crash (PRD malformed-response test)."""
    body = [
        {
            "id": 1,
            "name": "x",
            "status": "waiting",
            "repo_id": 1,
            "owner_id": 1,
            "task_id": 0,
            "attempt": 1,
            "handle": "h",
            # needs and runs_on intentionally absent
        },
        {
            "id": 2,
            "name": "y",
            "status": "waiting",
            "repo_id": 1,
            "owner_id": 1,
            "task_id": 0,
            "attempt": 1,
            "handle": "h2",
            "needs": None,
            "runs_on": None,
        },
    ]
    respx.get(
        "https://git.example.com/api/v1/admin/actions/runners/jobs"
    ).mock(return_value=httpx.Response(200, json=body))

    with fq.Client(_cfg()) as c:
        jobs = c.fetch_jobs()
    assert jobs[0].runs_on == () and jobs[0].needs == ()
    assert jobs[1].runs_on == () and jobs[1].needs == ()


# ---------------------------------------------------------------------------
# resolve_repo fallback contract
# ---------------------------------------------------------------------------


@respx.mock
def test_resolve_repo_404_returns_fallback_label():
    respx.get(
        "https://git.example.com/api/v1/repositories/999"
    ).mock(return_value=httpx.Response(404, json={"message": "not found"}))

    with fq.Client(_cfg()) as c:
        slug = c.resolve_repo(999)
    assert slug == "repo#999"


@respx.mock
def test_resolve_repo_403_returns_fallback_label():
    respx.get(
        "https://git.example.com/api/v1/repositories/777"
    ).mock(return_value=httpx.Response(403, json={"message": "forbidden"}))

    with fq.Client(_cfg()) as c:
        slug = c.resolve_repo(777)
    assert slug == "repo#777"


@respx.mock
def test_resolve_repo_timeout_returns_fallback_label():
    respx.get(
        "https://git.example.com/api/v1/repositories/555"
    ).mock(side_effect=httpx.TimeoutException("slow"))

    with fq.Client(_cfg()) as c:
        slug = c.resolve_repo(555)
    assert slug == "repo#555"


@respx.mock
def test_resolve_repo_success_returns_full_name_and_caches():
    route = respx.get(
        "https://git.example.com/api/v1/repositories/589"
    ).mock(
        return_value=httpx.Response(
            200, json={"id": 589, "full_name": "owner-a/repo-a"}
        )
    )
    with fq.Client(_cfg()) as c:
        first = c.resolve_repo(589)
        second = c.resolve_repo(589)
    assert first == "owner-a/repo-a" == second
    # Second call must be cached: only one HTTP request.
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Typed error mapping on the runners endpoint
# ---------------------------------------------------------------------------


@respx.mock
def test_runners_401_raises_auth_error():
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(401, json={"message": "nope"}))
    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.AuthError):
            c.fetch_runners()


@respx.mock
def test_runners_403_raises_auth_error():
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(403, json={"message": "needs admin"}))
    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.AuthError):
            c.fetch_runners()


@respx.mock
def test_runners_500_raises_connection_error():
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(503, text="upstream"))
    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.ConnectionError):
            c.fetch_runners()


@respx.mock
def test_runners_429_raises_connection_error():
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(429, text="slow down"))
    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.ConnectionError):
            c.fetch_runners()


@respx.mock
def test_runners_timeout_raises_connection_error():
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(side_effect=httpx.TimeoutException("slow"))
    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.ConnectionError):
            c.fetch_runners()


@respx.mock
def test_runners_connection_refused_raises_connection_error():
    """PRD Test Plan: connection-refused -> ConnectionError. httpx
    surfaces a refused TCP connect as `httpx.ConnectError`, which is a
    subclass of `httpx.HTTPError` (NOT TimeoutException). The client's
    `_get` must map it to our typed ConnectionError so M5 watch loop
    treats it as transient and M6 CLI exits 4.
    """
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(side_effect=httpx.ConnectError("Connection refused"))
    with fq.Client(_cfg()) as c:
        with pytest.raises(fq.ConnectionError):
            c.fetch_runners()


# ---------------------------------------------------------------------------
# Pagination boundary: exact-multiple and one-over (PRD M7 line 173).
# Default _RUNNER_PAGE_SIZE is 50.
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_runners_pagination_exact_page_size_multiple():
    """N == page_size: page 1 returns exactly 50 runners and a
    rel="next" link. Page 2 returns an empty list (no Link header).
    The client must walk both pages, end with 50 runners, and stop
    cleanly without raising.
    """
    page1 = [_runner_dict(i) for i in range(1, 51)]  # 50 runners
    next_link = (
        '<https://git.example.com/api/v1/admin/actions/runners'
        '?limit=50&page=2>; rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=page1, headers={"Link": next_link})

    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(side_effect=handler)

    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()
    assert len(runners) == 50
    assert [r.id for r in runners] == list(range(1, 51))


@respx.mock
def test_fetch_runners_pagination_one_over_page_size():
    """N == page_size + 1: page 1 returns 50, page 2 returns the
    remaining 1. The classic boundary case where a naive
    `if len(page) < limit: stop` would miss the last item.
    """
    page1 = [_runner_dict(i) for i in range(1, 51)]  # 50 runners
    page2 = [_runner_dict(51)]                       # 1 runner
    next_link = (
        '<https://git.example.com/api/v1/admin/actions/runners'
        '?limit=50&page=2>; rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1, headers={"Link": next_link})

    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(side_effect=handler)

    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()
    assert len(runners) == 51
    assert [r.id for r in runners] == list(range(1, 52))


# ---------------------------------------------------------------------------
# Malformed runner / partial-shape tolerance (PRD M7 line 173).
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_runners_tolerates_missing_optional_fields():
    """A runner row missing `name`/`status`/`version`/`labels`/`ephemeral`
    must NOT crash the client. Required field is just `id`; the
    normalizer defaults everything else (status -> 'offline', labels
    -> (), ephemeral -> False). Symmetric with the existing job-side
    test_fetch_jobs_tolerates_missing_optional_fields.
    """
    body = [
        {"id": 100},                                  # bare minimum
        {"id": 101, "labels": None, "status": None},  # explicit nulls
        {"id": 102, "name": "anon", "labels": ["grunt"]},
    ]
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(200, json=body))

    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()
    assert [r.id for r in runners] == [100, 101, 102]
    assert runners[0].labels == ()
    assert runners[0].ephemeral is False
    assert runners[1].labels == ()
    assert runners[2].labels == ("grunt",)


@respx.mock
def test_fetch_runners_unexpected_status_string_does_not_crash():
    """A runner with a status the M2 layer doesn't know about (some
    new Forgejo state e.g. 'maintenance') must reach the aggregator
    unchanged. M2's `_is_online({"active", "idle"})` will then treat
    it as offline; the client must NOT veto on the unknown string.
    """
    body = [
        {
            "id": 200,
            "name": "weird",
            "status": "maintenance",   # not active/idle/offline
            "version": "v12.7.3",
            "labels": ["grunt"],
            "ephemeral": False,
        }
    ]
    respx.get(
        url__regex=r"https://git\.wxs\.ro/api/v1/admin/actions/runners.*"
    ).mock(return_value=httpx.Response(200, json=body))

    with fq.Client(_cfg()) as c:
        runners = c.fetch_runners()
    assert runners[0].status == "maintenance"


# ---------------------------------------------------------------------------
# Link header parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, None),
        ("", None),
        ('<https://x/y?page=2>; rel="next"', "https://x/y?page=2"),
        (
            '<https://x/y?page=2>; rel="next", <https://x/y?page=5>; rel="last"',
            "https://x/y?page=2",
        ),
        ('<https://x/y?page=5>; rel="last"', None),
        ("<https://x/y?page=2>; rel=next", "https://x/y?page=2"),
    ],
)
def test_parse_next_link(header, expected):
    assert fq._parse_next_link(header) == expected


# ---------------------------------------------------------------------------
# Prometheus metrics client (fetch_runner_pods).
#
# The 3 instant queries all hit /api/v1/query with a different `query`
# param. We dispatch on the metric name embedded in the query string.
# Graceful contract: ANY failure returns ([], <error str>) and NEVER
# raises. The Prometheus client must be isolated from the Forgejo client:
# no Authorization header is ever sent.
# ---------------------------------------------------------------------------

METRICS_URL = "https://prometheus.example.com"


def _prom_fixture() -> dict:
    return json.loads((FIXTURES / "prometheus-live.json").read_text())


def _prom_handler(fixture: dict):
    """Return a respx side_effect that maps each PromQL query to its
    matching fixture response (cpu / memory / pod-info)."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if "container_cpu_usage_seconds_total" in query:
            return httpx.Response(200, json=fixture["cpu"])
        if "container_memory_working_set_bytes" in query:
            return httpx.Response(200, json=fixture["memory"])
        if "kube_pod_container_resource_limits" in query:
            if 'resource="memory"' in query:
                return httpx.Response(200, json=fixture["memory_limit"])
            if 'resource="cpu"' in query:
                return httpx.Response(200, json=fixture["cpu_limit"])
            return httpx.Response(400, text="unexpected limit query")
        if "kube_pod_info" in query:
            return httpx.Response(200, json=fixture["info"])
        return httpx.Response(400, text="unexpected query")

    return handler


@respx.mock
def test_fetch_runner_pods_joins_three_queries_by_pod():
    """The 3 vectors join by pod into typed PodResource rows: cpu_cores
    is a float, memory_bytes an int (from the float-as-string), node is
    attached from kube_pod_info. Sorted by pod name.
    """
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=_prom_handler(_prom_fixture()))

    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert error is None
    assert len(pods) == 3
    by_pod = {p.pod: p for p in pods}
    qf = by_pod["ci-runner-aaaa1111ff-pod1"]
    assert isinstance(qf.cpu_cores, float)
    assert isinstance(qf.memory_bytes, int)
    assert qf.cpu_cores == pytest.approx(0.00109)
    assert qf.memory_bytes == 763322368
    assert qf.node == "k8s-node-2"
    # Memory limit IS set (~17.4 GiB); CPU limit is NOT (None).
    assert qf.memory_limit_bytes == 18674094196
    assert qf.cpu_limit_cores is None
    # Deterministic order: sorted by pod name.
    assert [p.pod for p in pods] == sorted(p.pod for p in pods)


@respx.mock
def test_fetch_runner_pods_memory_limit_present_cpu_limit_absent():
    """Live reality: every pod has a memory limit but no CPU limit. The
    memory_limit_bytes field is set for all; cpu_limit_cores is None for
    all (the cpu-limit query returns an empty vector).
    """
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=_prom_handler(_prom_fixture()))

    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert error is None
    assert all(p.memory_limit_bytes == 18674094196 for p in pods)
    assert all(isinstance(p.memory_limit_bytes, int) for p in pods)
    assert all(p.cpu_limit_cores is None for p in pods)


@respx.mock
def test_fetch_runner_pods_limit_present_for_cpu_when_set():
    """If a CPU limit IS configured, it joins as a float. Synthesize a
    non-empty cpu-limit vector to exercise the present-path.
    """
    fixture = _prom_fixture()
    fixture["cpu_limit"] = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"pod": "ci-runner-aaaa1111ff-pod1"},
                    "value": [1780052001.832, "0.5"],
                }
            ],
        },
    }
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=_prom_handler(fixture))

    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert error is None
    by_pod = {p.pod: p for p in pods}
    qf = by_pod["ci-runner-aaaa1111ff-pod1"]
    assert qf.cpu_limit_cores == pytest.approx(0.5)
    assert isinstance(qf.cpu_limit_cores, float)
    # Pods absent from the cpu-limit query keep None.
    assert by_pod["ci-runner-aaaa1111ff-pod2"].cpu_limit_cores is None


@respx.mock
def test_fetch_runner_pods_pod_missing_from_memory_limit_query_is_none():
    """A pod present in usage but absent from the memory-limit query has
    memory_limit_bytes == None (the join is left-outer on the usage pods).
    """
    fixture = _prom_fixture()
    # Drop the last pod (b5f9b) from the memory-limit result.
    fixture["memory_limit"]["data"]["result"] = (
        fixture["memory_limit"]["data"]["result"][:2]
    )
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=_prom_handler(fixture))

    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert error is None
    by_pod = {p.pod: p for p in pods}
    assert by_pod["ci-runner-aaaa1111ff-pod1"].memory_limit_bytes == 18674094196
    assert by_pod["ci-runner-aaaa1111ff-pod2"].memory_limit_bytes is None


@respx.mock
def test_fetch_runner_pods_no_auth_header_sent_to_prometheus():
    """The Prometheus client is isolated from the Forgejo client: it
    must NEVER carry an Authorization header (the admin token must not
    leak to Prometheus).
    """
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return _prom_handler(_prom_fixture())(request)

    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=handler)

    fq.fetch_runner_pods(METRICS_URL)
    assert seen, "no prometheus request captured"
    assert all(h is None for h in seen), f"auth header leaked: {seen}"


@respx.mock
def test_fetch_runner_pods_cluster_green_filters_by_node_prefix():
    """--metrics-cluster green keeps only pods on k8s-node-* nodes."""
    fixture = _prom_fixture()
    # Repoint one pod onto a blue node so the filter has something to drop.
    fixture["info"]["data"]["result"][2]["metric"]["node"] = "k8s-node-4"
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=_prom_handler(fixture))

    green, gerr = fq.fetch_runner_pods(METRICS_URL, cluster="green")
    assert gerr is None
    assert {p.pod for p in green} == {
        "ci-runner-aaaa1111ff-pod1",
        "ci-runner-aaaa1111ff-pod3",
    }
    assert all(p.node.startswith("k8s-node-") for p in green)


@respx.mock
def test_fetch_runner_pods_cluster_blue_filters_by_node_prefix():
    fixture = _prom_fixture()
    fixture["info"]["data"]["result"][2]["metric"]["node"] = "k8s-node-4"
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=_prom_handler(fixture))

    blue, berr = fq.fetch_runner_pods(METRICS_URL, cluster="blue")
    assert berr is None
    assert {p.pod for p in blue} == {"ci-runner-aaaa1111ff-pod2"}


@respx.mock
def test_fetch_runner_pods_auto_applies_no_filter():
    fixture = _prom_fixture()
    fixture["info"]["data"]["result"][2]["metric"]["node"] = "k8s-node-4"
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=_prom_handler(fixture))

    pods, error = fq.fetch_runner_pods(METRICS_URL, cluster="auto")
    assert error is None
    assert len(pods) == 3


@respx.mock
def test_fetch_runner_pods_passes_namespace_into_queries():
    """A custom --metrics-namespace is substituted into all 3 queries."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("query", ""))
        return _prom_handler(_prom_fixture())(request)

    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=handler)

    fq.fetch_runner_pods(METRICS_URL, namespace="ci-runners")
    assert captured
    assert all('namespace="ci-runners"' in q for q in captured)


@respx.mock
def test_fetch_runner_pods_http_500_is_graceful():
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(return_value=httpx.Response(500, text="boom"))
    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert pods == ()
    assert error is not None and "500" in error


@respx.mock
def test_fetch_runner_pods_timeout_is_graceful():
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=httpx.TimeoutException("slow"))
    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert pods == ()
    assert error is not None and "timeout" in error.lower()


@respx.mock
def test_fetch_runner_pods_connection_refused_is_graceful():
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(side_effect=httpx.ConnectError("refused"))
    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert pods == ()
    assert error is not None


@respx.mock
def test_fetch_runner_pods_malformed_body_is_graceful():
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(return_value=httpx.Response(200, text="not json{{"))
    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert pods == ()
    assert error is not None


@respx.mock
def test_fetch_runner_pods_status_not_success_is_graceful():
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "errorType": "bad_data"}
        )
    )
    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert pods == ()
    assert error is not None


@respx.mock
def test_fetch_runner_pods_empty_vectors_returns_no_pods_no_error():
    """A healthy Prometheus with zero matching series is success, not an
    error: empty pods, error None.
    """
    empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
    respx.get(
        url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*"
    ).mock(return_value=httpx.Response(200, json=empty))
    pods, error = fq.fetch_runner_pods(METRICS_URL)
    assert pods == ()
    assert error is None


# ---------------------------------------------------------------------------
# NCPS status (fetch_ncps_status). Same isolated no-auth client; 4 scalar
# queries; never-raises -> None on any failure.
# ---------------------------------------------------------------------------


def _scalar_response(value):
    """A Prometheus scalar-vector response, or an empty vector for None."""
    result = [] if value is None else [{"metric": {}, "value": [1.0, str(value)]}]
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
    )


def _ncps_handler(*, req, inflight, upstream, bytes_):
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        if "request_duration_millis_milliseconds_count" in q:
            return _scalar_response(req)
        if "requests_inflight" in q:
            return _scalar_response(inflight)
        if "http_client_request_duration_seconds_count" in q:
            return _scalar_response(upstream)
        if "container_network_transmit_bytes_total" in q:
            return _scalar_response(bytes_)
        return httpx.Response(400, text="unexpected")

    return handler


@respx.mock
def test_fetch_ncps_status_active_from_fixture():
    """Joins the 4 scalar queries; types are float/int; active when
    requests_per_sec > 0. Uses the committed fixture's active sample.
    """
    fixture = _prom_fixture()
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        if "request_duration_millis_milliseconds_count" in q:
            return httpx.Response(200, json=fixture["ncps_requests"])
        if "requests_inflight" in q:
            return httpx.Response(200, json=fixture["ncps_inflight"])
        if "http_client_request_duration_seconds_count" in q:
            return httpx.Response(200, json=fixture["ncps_upstream"])
        if "container_network_transmit_bytes_total" in q:
            return httpx.Response(200, json=fixture["ncps_bytes"])
        return httpx.Response(400, text="unexpected")

    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=handler
    )
    n = fq.fetch_ncps_status(METRICS_URL)
    assert n is not None
    assert n.active is True
    assert n.requests_per_sec == pytest.approx(8.5)
    assert n.inflight == 1 and isinstance(n.inflight, int)
    assert n.upstream_per_sec == pytest.approx(0.3)
    assert n.bytes_per_sec == pytest.approx(4000000.0)


@respx.mock
def test_fetch_ncps_status_active_when_only_inflight_nonzero():
    """req/s == 0 but inflight > 0 -> active."""
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=_ncps_handler(req=0, inflight=2, upstream=0, bytes_=0)
    )
    n = fq.fetch_ncps_status(METRICS_URL)
    assert n is not None and n.active is True
    assert n.requests_per_sec == 0.0 and n.inflight == 2


@respx.mock
def test_fetch_ncps_status_active_when_only_requests_nonzero():
    """inflight == 0 but req/s > 0 -> active. Isolates the req>0 arm of the
    `req > 0 or inflight > 0` OR (the fixture active sample has BOTH nonzero,
    so on its own it can't prove the request arm flips active independently).
    """
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=_ncps_handler(req=5.0, inflight=0, upstream=0, bytes_=0)
    )
    n = fq.fetch_ncps_status(METRICS_URL)
    assert n is not None and n.active is True
    assert n.requests_per_sec == 5.0 and n.inflight == 0


@respx.mock
def test_fetch_ncps_status_idle_when_both_zero():
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=_ncps_handler(req=0, inflight=0, upstream=0, bytes_=0)
    )
    n = fq.fetch_ncps_status(METRICS_URL)
    assert n is not None and n.active is False


@respx.mock
def test_fetch_ncps_status_missing_metric_treated_as_zero():
    """An empty result vector (metric not scraped yet) -> 0.0, not an
    error. With all-empty the status is idle.
    """
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=_ncps_handler(req=None, inflight=None, upstream=None, bytes_=None)
    )
    n = fq.fetch_ncps_status(METRICS_URL)
    assert n is not None
    assert n.active is False
    assert n.requests_per_sec == 0.0 and n.inflight == 0
    assert n.upstream_per_sec == 0.0 and n.bytes_per_sec == 0.0


@respx.mock
def test_fetch_ncps_status_nan_treated_as_zero():
    """Prometheus emits "NaN" for an empty rate window -> 0.0."""
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=_ncps_handler(req="NaN", inflight=0, upstream="NaN", bytes_="NaN")
    )
    n = fq.fetch_ncps_status(METRICS_URL)
    assert n is not None
    assert n.requests_per_sec == 0.0 and n.bytes_per_sec == 0.0 and n.active is False


@respx.mock
def test_fetch_ncps_status_http_error_returns_none():
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        return_value=httpx.Response(500, text="boom")
    )
    assert fq.fetch_ncps_status(METRICS_URL) is None


@respx.mock
def test_fetch_ncps_status_timeout_returns_none():
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=httpx.TimeoutException("slow")
    )
    assert fq.fetch_ncps_status(METRICS_URL) is None


@respx.mock
def test_fetch_ncps_status_malformed_body_returns_none():
    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        return_value=httpx.Response(200, text="not json{{")
    )
    assert fq.fetch_ncps_status(METRICS_URL) is None


@respx.mock
def test_fetch_ncps_status_no_auth_header_sent():
    """Isolation: the NCPS queries carry no Authorization header."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return _scalar_response(0)

    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=handler
    )
    fq.fetch_ncps_status(METRICS_URL)
    assert seen and all(h is None for h in seen)


@respx.mock
def test_fetch_ncps_status_interpolates_rate_window():
    """The 2m window is interpolated into the rate() queries (single
    source of truth via _NCPS_RATE_WINDOW).
    """
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("query", ""))
        return _scalar_response(0)

    respx.get(url__regex=r"https://prometheus\.wxs\.ro/api/v1/query.*").mock(
        side_effect=handler
    )
    fq.fetch_ncps_status(METRICS_URL)
    rate_queries = [q for q in captured if "rate(" in q]
    assert rate_queries
    assert all(f"[{fq._NCPS_RATE_WINDOW}]" in q for q in rate_queries)
