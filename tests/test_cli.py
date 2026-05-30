"""M6 CLI tests.

PRD #61 §M6 + §Run modes + §Output formats. Covers argparse wiring,
mode resolution (json forces once; TTY default), --schema / --version
early exits, json stdout purity, the JSON error envelope on failure,
typed exit codes, --repo / --label client-side scoping, and the
mutually-exclusive mode group.

main() takes injectable seams (`client`, `stdout`, `stderr`, `is_tty`)
so the whole dispatch can be exercised without a real network or a
real terminal.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import httpx
import pytest
import respx

import fj_queue as fq


FIXTURES = Path(__file__).resolve().parent / "fixtures"


HOST = "git.example.com"


# ---------------------------------------------------------------------------
# A scripted Client (mirrors the M5 FakeClient, single-shot here).
# ---------------------------------------------------------------------------


def _runner(rid, *, status="active", labels=("grunt",), name=None):
    return fq.Runner(
        id=rid,
        name=name or f"runner-{rid}",
        status=status,
        version="v12.7.3",
        labels=tuple(labels),
        ephemeral=False,
    )


def _job(jid, *, status="waiting", repo_id=85, runs_on=("grunt",), needs=(), task_id=0, name=None):
    return fq.RawJob(
        id=jid,
        name=name or f"job-{jid}",
        status=status,
        repo_id=repo_id,
        owner_id=1,
        runs_on=tuple(runs_on),
        needs=tuple(needs),
        task_id=task_id,
        attempt=1,
        handle=f"h-{jid}",
    )


class FakeClient:
    """Single-tick fake. `error` (if set) raises on fetch_runners."""

    def __init__(self, runners=None, jobs=None, repos=None, error=None):
        self._runners = list(runners or [])
        self._jobs = list(jobs or [])
        self._repos = dict(repos or {})
        self._error = error
        self.closed = False

    def fetch_runners(self):
        if self._error is not None:
            raise self._error
        return list(self._runners)

    def fetch_jobs(self):
        return list(self._jobs)

    def resolve_repo(self, repo_id):
        return self._repos.get(repo_id, f"repo#{repo_id}")

    def close(self):
        self.closed = True


def _typical_client():
    return FakeClient(
        runners=[
            _runner(3, name="k8s-runner", status="active", labels=("docker", "grunt")),
            _runner(2, name="k8s-runner-b", status="offline", labels=("docker",)),
        ],
        jobs=[
            _job(80239, status="waiting", repo_id=85, runs_on=("grunt",), name="Semantic Release"),
            _job(79969, status="waiting", repo_id=586, runs_on=("special",), name="pipeline"),
            _job(83886, status="running", task_id=85492, repo_id=589, runs_on=("grunt",), name="Chainsaw"),
        ],
        repos={85: "owner-c/theme-api", 586: "owner-b/harbor", 589: "owner-a/repo-a"},
    )


def _run(argv, *, client=None, is_tty=False, env_token="tok"):
    """Invoke main() with captured streams. Returns (rc, stdout, stderr).

    Metrics are OFF by default (M2). Tests that exercise the enabled path
    explicitly pass --metrics-url + their own respx mock.
    """
    out, err = io.StringIO(), io.StringIO()
    # main() resolves the token; default to passing --token so tests
    # don't depend on the ambient environment.
    if env_token is not None and "--token" not in argv and "--schema" not in argv:
        argv = [*argv, "--token", env_token]
    # main() now requires a host (no built-in default). Supply HOST so
    # tests that do not exercise host-resolution behavior stay working.
    # Tests that explicitly pass --host override this injection.
    if "--host" not in argv and "--schema" not in argv:
        argv = [*argv, "--host", HOST]
    # Isolate from any stray ./fj-queue.toml in the developer's cwd and from
    # ~/.config/fj-queue/config.toml (M4 GAP1). /dev/null is always empty
    # so tomllib returns {} -- identical to "no config file found".
    if "--config" not in argv and "--schema" not in argv:
        argv = [*argv, "--config", os.devnull]
    rc = fq.main(argv, client=client, stdout=out, stderr=err, is_tty=is_tty)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# --schema / --version early exits.
# ---------------------------------------------------------------------------


def test_schema_flag_prints_schema_and_exits_zero():
    rc, out, err = _run(["--schema"], env_token=None)
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["title"] == "fj-queue v1 snapshot"
    # No token needed for --schema.
    assert err == ""


def test_version_flag_exits_zero(capsys):
    # argparse --version writes to the real stdout and raises
    # SystemExit(0); main() catches and returns 0.
    rc = fq.main(["--version"], stdout=io.StringIO(), stderr=io.StringIO())
    assert rc == fq.EXIT_OK
    captured = capsys.readouterr()
    assert f"fj-queue {fq.__version__}" in captured.out


# ---------------------------------------------------------------------------
# Mutually-exclusive mode group.
# ---------------------------------------------------------------------------


def test_watch_and_once_are_mutually_exclusive():
    rc, out, err = _run(["--watch", "--once"])
    assert rc == fq.EXIT_USAGE  # argparse usage error


def test_mode_and_watch_alias_mutually_exclusive():
    rc, out, err = _run(["--mode", "once", "--watch"])
    assert rc == fq.EXIT_USAGE


# ---------------------------------------------------------------------------
# Mode resolution.
# ---------------------------------------------------------------------------


def test_json_format_forces_once_even_with_watch_flag():
    """--format json + --watch: json wins, warning to stderr, single
    snapshot emitted (no live loop).
    """
    rc, out, err = _run(
        ["--format", "json", "--watch"],
        client=_typical_client(),
        is_tty=True,
    )
    assert rc == fq.EXIT_OK
    assert "forces --once" in err
    # stdout parses as a single JSON document.
    parsed = json.loads(out)
    assert parsed["schema_version"] == 1


def test_default_mode_once_when_not_tty():
    """No explicit mode + piped stdout (is_tty False) -> once (no
    infinite loop on `fj-queue > file`)."""
    rc, out, err = _run(["--format", "plain"], client=_typical_client(), is_tty=False)
    assert rc == fq.EXIT_OK
    assert "TOTALS:" in out  # rendered a single snapshot


# ---------------------------------------------------------------------------
# json stdout purity + error envelope.
# ---------------------------------------------------------------------------


def test_json_success_stdout_is_pure_json():
    """PRD: on success stdout carries ONLY the JSON document. The whole
    stdout must parse with no leading/trailing junk.
    """
    rc, out, err = _run(["--format", "json"], client=_typical_client())
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)  # raises if any non-JSON noise present
    assert parsed["host"] == HOST
    assert parsed["totals"]["total"] == 3
    # Diagnostics (if any) are on stderr; success has none.
    assert err == ""


def test_json_error_envelope_on_auth_failure():
    """AuthError under --format json: JSON error object on stdout,
    diagnostic on stderr, exit 3.
    """
    client = FakeClient(error=fq.AuthError("403 forbidden"))
    rc, out, err = _run(["--format", "json"], client=client)
    assert rc == fq.EXIT_AUTH
    parsed = json.loads(out)
    assert parsed["error"]["code"] == "auth"
    assert parsed["error"]["host"] == HOST
    assert "fj-queue:" in err


def test_json_error_envelope_on_connection_failure():
    client = FakeClient(error=fq.ConnectionError("timeout"))
    rc, out, err = _run(["--format", "json"], client=client)
    assert rc == fq.EXIT_CONNECTION
    parsed = json.loads(out)
    assert parsed["error"]["code"] == "connection"


def test_missing_token_exit_2(monkeypatch):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--host", HOST],
        client=_typical_client(),
        stdout=out,
        stderr=err,
        is_tty=False,
    )
    assert rc == fq.EXIT_USAGE
    parsed = json.loads(out.getvalue())
    assert parsed["error"]["code"] == "usage"


def test_main_scrubs_token_from_json_error_envelope():
    """The M5/M6 caller the scrub_token kwarg was built for: main()
    passes config.token to render_json_error, so a token that leaks
    into an exception message is masked in the stdout envelope.
    """
    secret = "SUPERSECRET_TOKEN_ABC123"
    client = FakeClient(error=fq.ConnectionError(f"boom with {secret} in it"))
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--once", "--token", secret, "--host", HOST],
        client=client, stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_CONNECTION
    rendered = out.getvalue()
    parsed = json.loads(rendered)
    assert secret not in rendered
    assert "***" in parsed["error"]["message"]


def test_main_token_arg_beats_env_proven_via_scrub(monkeypatch):
    """End-to-end token precedence: --token X beats $FORGEJO_TOKEN Y.
    Proof by scrubbing: only the RESOLVED token is masked in the error
    envelope. If env had won, the arg token would survive and the env
    token would be masked. Here the arg token is masked (it won) and
    the env token survives verbatim.
    """
    monkeypatch.setenv("FORGEJO_TOKEN", "ENV_TOKEN_Y")
    arg_token = "ARG_TOKEN_X"
    client = FakeClient(
        error=fq.ConnectionError(f"fail with {arg_token} and ENV_TOKEN_Y")
    )
    out, err = io.StringIO(), io.StringIO()
    fq.main(
        ["--format", "json", "--once", "--token", arg_token, "--host", HOST],
        client=client, stdout=out, stderr=err, is_tty=False,
    )
    rendered = out.getvalue()
    assert arg_token not in rendered          # resolved token -> scrubbed
    assert "ENV_TOKEN_Y" in rendered          # not resolved -> survives


def test_main_env_token_used_when_no_arg(monkeypatch):
    """No --token: $FORGEJO_TOKEN is the resolved token (and thus the
    one scrubbed).
    """
    monkeypatch.setenv("FORGEJO_TOKEN", "ENV_ONLY_TOKEN")
    client = FakeClient(
        error=fq.ConnectionError("fail with ENV_ONLY_TOKEN inside")
    )
    out, err = io.StringIO(), io.StringIO()
    fq.main(
        ["--format", "json", "--once", "--host", HOST],
        client=client, stdout=out, stderr=err, is_tty=False,
    )
    rendered = out.getvalue()
    assert "ENV_ONLY_TOKEN" not in rendered   # env token scrubbed


def test_plain_error_goes_to_stderr_not_stdout():
    """In rich/plain mode, a failure prints to stderr; stdout stays
    empty (no half-rendered dashboard).
    """
    client = FakeClient(error=fq.ConnectionError("timeout"))
    rc, out, err = _run(["--format", "plain"], client=client)
    assert rc == fq.EXIT_CONNECTION
    assert out == ""
    assert "fj-queue:" in err


# ---------------------------------------------------------------------------
# --repo / --label client-side scoping.
# ---------------------------------------------------------------------------


def test_repo_filter_scopes_queue_only_totals_stay_instance_wide():
    """--repo scopes Snapshot.queue ONLY. totals, per_repo, runners,
    warnings, schedulable_labels stay instance-wide (PRD §JSON contract
    line 94, §Progress checkpoint M6 line 159).

    _typical_client() has 3 jobs (2 waiting + 1 running) across 3
    repos; --repo owner-c/theme-api keeps only that repo's job in
    queue but the global totals remain unchanged.
    """
    rc, out, err = _run(
        ["--format", "json", "--repo", "owner-c/theme-api"],
        client=_typical_client(),
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["filter"]["repo"] == "owner-c/theme-api"
    # totals are instance-wide: 2 waiting + 1 running across the 3 jobs.
    assert parsed["totals"] == {"running": 1, "waiting": 2, "total": 3}
    # per_repo lists ALL repos, not just the filtered one.
    repos_in_breakdown = {r["repo"] for r in parsed["per_repo"]}
    assert repos_in_breakdown == {
        "owner-c/theme-api",
        "owner-b/harbor",
        "owner-a/repo-a",
    }
    # queue is scoped to the requested repo only.
    assert all(j["repo"] == "owner-c/theme-api" for j in parsed["queue"])
    assert len(parsed["queue"]) == 1


def test_repo_filter_preserves_global_positions_with_gaps():
    """The surviving queue entries keep their GLOBAL position (1-based
    over the unfiltered waiting set). Positions can have gaps.
    """
    # 5 waiting jobs across two repos, interleaved by job_id (which is
    # the FIFO-approximation order aggregate uses).
    client = FakeClient(
        runners=[_runner(1, status="active", labels=("grunt",))],
        jobs=[
            _job(10, repo_id=85, runs_on=("grunt",)),   # alpha pos 1
            _job(20, repo_id=99, runs_on=("grunt",)),   # other pos 2
            _job(30, repo_id=85, runs_on=("grunt",)),   # alpha pos 3
            _job(40, repo_id=99, runs_on=("grunt",)),   # other pos 4
            _job(50, repo_id=85, runs_on=("grunt",)),   # alpha pos 5
        ],
        repos={85: "alpha/repo", 99: "other/repo"},
    )
    rc, out, err = _run(["--format", "json", "--repo", "alpha/repo"], client=client)
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    positions = [j["position"] for j in parsed["queue"]]
    assert positions == [1, 3, 5]  # gaps preserved, NOT renumbered to [1, 2, 3]
    assert parsed["totals"]["waiting"] == 5  # unfiltered waiting count


def test_repo_filter_keeps_warnings_for_other_repos():
    """Warnings about unschedulable jobs in a NON-filtered repo still
    surface when --repo X is set for a different X. Schedulability is
    an instance-wide property.
    """
    client = FakeClient(
        runners=[_runner(1, status="active", labels=("grunt",))],
        jobs=[
            _job(100, repo_id=85, runs_on=("grunt",), name="ok-in-x"),
            # repo 99 needs a label no runner offers -> unschedulable.
            _job(200, repo_id=99, runs_on=("gpu",), name="stuck-in-y"),
        ],
        repos={85: "alpha/repo", 99: "stuck/repo"},
    )
    rc, out, err = _run(
        ["--format", "json", "--repo", "alpha/repo"],
        client=client,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    # queue is scoped to alpha/repo
    assert {j["repo"] for j in parsed["queue"]} == {"alpha/repo"}
    # but the warning for stuck/repo's job still surfaces
    warning_repos = {w["repo"] for w in parsed["warnings"]}
    assert "stuck/repo" in warning_repos


def test_label_filter_subset_semantics():
    """--label keeps jobs whose runs_on includes at least the label.
    Job 80239 (runs_on=[grunt]) matches --label grunt; 79969
    (runs_on=[special]) does not. totals stay instance-wide.
    """
    rc, out, err = _run(
        ["--format", "json", "--label", "grunt"],
        client=_typical_client(),
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["filter"]["label"] == ["grunt"]
    waiting_ids = {j["job_id"] for j in parsed["queue"]}
    assert 80239 in waiting_ids
    assert 79969 not in waiting_ids
    # totals + per_repo remain instance-wide regardless of --label.
    assert parsed["totals"]["waiting"] == 2
    assert parsed["totals"]["total"] == 3
    assert {r["repo"] for r in parsed["per_repo"]} == {
        "owner-c/theme-api",
        "owner-b/harbor",
        "owner-a/repo-a",
    }


def test_label_filter_preserves_global_positions():
    """--label scoping also preserves global queue positions (gaps
    expected when filtered jobs sit between matched ones).
    """
    client = FakeClient(
        runners=[_runner(1, status="active", labels=("grunt", "docker"))],
        jobs=[
            _job(10, runs_on=("grunt",)),               # pos 1, matches grunt
            _job(20, runs_on=("special",)),             # pos 2, no match
            _job(30, runs_on=("grunt", "docker")),      # pos 3, matches grunt
            _job(40, runs_on=("docker",)),              # pos 4, no match
        ],
        repos={85: "x/y"},
    )
    rc, out, err = _run(["--format", "json", "--label", "grunt"], client=client)
    parsed = json.loads(out)
    positions = [j["position"] for j in parsed["queue"]]
    assert positions == [1, 3]
    assert parsed["totals"]["waiting"] == 4


def test_label_repeatable_requires_all():
    """Multiple --label values: subset semantics means a job must carry
    ALL requested labels. A job with only [grunt] does NOT match
    --label grunt --label docker.
    """
    client = FakeClient(
        runners=[_runner(1, status="active", labels=("docker", "grunt"))],
        jobs=[
            _job(1, runs_on=("grunt",), name="grunt-only"),
            _job(2, runs_on=("grunt", "docker"), name="both"),
        ],
        repos={85: "x/y"},
    )
    rc, out, err = _run(
        ["--format", "json", "--label", "grunt", "--label", "docker"],
        client=client,
    )
    parsed = json.loads(out)
    ids = {j["job_id"] for j in parsed["queue"]}
    assert ids == {2}  # only the job carrying both labels
    assert parsed["filter"]["label"] == ["grunt", "docker"]


def test_no_filter_echoes_null():
    rc, out, err = _run(["--format", "json"], client=_typical_client())
    parsed = json.loads(out)
    assert parsed["filter"] == {"repo": None, "label": None}


# ---------------------------------------------------------------------------
# plain / rich once dispatch render to stdout.
# ---------------------------------------------------------------------------


def test_plain_once_renders_to_stdout():
    rc, out, err = _run(["--format", "plain", "--once"], client=_typical_client())
    assert rc == fq.EXIT_OK
    assert "RUNNERS (" in out
    assert "QUEUE (" in out


def test_rich_once_renders_to_stdout():
    rc, out, err = _run(["--format", "rich", "--once"], client=_typical_client())
    assert rc == fq.EXIT_OK
    # Rich Console output includes the snapshot panel title.
    assert "snapshot" in out


# ---------------------------------------------------------------------------
# Config plumbing: host / timeout / token reach the Config.
# ---------------------------------------------------------------------------


def test_host_flag_reaches_json_output():
    rc, out, err = _run(
        ["--format", "json", "--host", "git.example.com"],
        client=_typical_client(),
    )
    parsed = json.loads(out)
    assert parsed["host"] == "git.example.com"


# ---------------------------------------------------------------------------
# Parser-level checks.
# ---------------------------------------------------------------------------


def test_parser_defaults():
    parser = fq._build_parser()
    args = parser.parse_args([])
    assert args.mode is None
    assert args.format == "rich"
    assert args.interval == 2.0
    # --host no longer has a built-in default; resolve_host() is
    # required to supply a host before Config is constructed.
    assert args.host is None
    # --config: no default (auto-discovery runs when None)
    assert args.config is None
    assert args.timeout == 10.0
    assert args.label is None
    assert args.repo is None
    assert args.schema is False
    # --metrics/--no-metrics: None means "neither flag given; check config"
    assert args.metrics is None
    # --node-prefix replaces --metrics-cluster; default None (no filter)
    assert args.node_prefix is None


def test_parser_watch_alias_sets_mode():
    parser = fq._build_parser()
    assert parser.parse_args(["--watch"]).mode == "watch"
    assert parser.parse_args(["--once"]).mode == "once"
    assert parser.parse_args(["--mode", "watch"]).mode == "watch"


# ---------------------------------------------------------------------------
# M6 polish: symmetric token scrub on stderr + Rich error panel.
# ---------------------------------------------------------------------------


def test_stderr_diagnostic_scrubs_token_in_plain_mode():
    """Audit finding (HIGH): _emit_error must scrub the token from the
    stderr diagnostic, symmetric with the JSON envelope path. A
    `--format plain` run that hits an exception carrying the token in
    its message must NOT leak the literal token to stderr.
    """
    secret = "SUPERSECRET_TOKEN_PLAIN"
    client = FakeClient(error=fq.ConnectionError(f"boom with {secret} embedded"))
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "plain", "--once", "--token", secret, "--host", HOST],
        client=client, stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_CONNECTION
    err_text = err.getvalue()
    assert secret not in err_text
    assert "***" in err_text
    assert "fj-queue:" in err_text  # prefix still there


def test_stderr_diagnostic_scrubs_token_in_json_mode():
    """The JSON mode already scrubs stdout; the stderr line emitted
    alongside the envelope must also scrub (defense in depth).
    """
    secret = "SUPERSECRET_TOKEN_JSON_STDERR"
    client = FakeClient(error=fq.ConnectionError(f"fail near {secret} now"))
    out, err = io.StringIO(), io.StringIO()
    fq.main(
        ["--format", "json", "--once", "--token", secret, "--host", HOST],
        client=client, stdout=out, stderr=err, is_tty=False,
    )
    assert secret not in err.getvalue()
    assert secret not in out.getvalue()
    assert "***" in err.getvalue()


def test_build_error_renderable_scrubs_token():
    """Watch mid-loop error panel (`_build_error_renderable`) must
    mask the token in `str(exc)` before composing the Rich Text body,
    symmetric with the JSON envelope and stderr paths.
    """
    from rich.console import Console as _Console

    secret = "SUPERSECRET_WATCH_PANEL"
    exc = fq.AuthError(f"401 with leaked {secret} in body")
    panel = fq._build_error_renderable(
        exc,
        title="auth error",
        headline="Authentication failed.",
        scrub_token=secret,
    )
    buf = io.StringIO()
    _Console(file=buf, width=120, force_terminal=False, no_color=True).print(panel)
    rendered = buf.getvalue()
    assert secret not in rendered
    assert "***" in rendered


def test_build_error_renderable_no_scrub_when_token_falsy():
    """Sanity: scrub_token=None is a no-op (no accidental masking of
    unrelated strings).
    """
    from rich.console import Console as _Console

    exc = fq.AuthError("plain auth error message")
    panel = fq._build_error_renderable(
        exc, title="auth error", headline="Boom.", scrub_token=None
    )
    buf = io.StringIO()
    _Console(file=buf, width=120, force_terminal=False, no_color=True).print(panel)
    assert "plain auth error message" in buf.getvalue()


# ---------------------------------------------------------------------------
# M6 polish: malformed --host is mapped to a typed ConfigError (exit 2),
# not an untyped httpx.InvalidURL that bypasses the JSON envelope.
# ---------------------------------------------------------------------------


def test_malformed_host_crlf_returns_typed_usage_error_json():
    """Audit finding (MEDIUM): a `--host` value containing CRLF (or
    any other httpx.InvalidURL-trigger) must surface as ConfigError
    (exit 2) with a parseable JSON error envelope on stdout. Before
    the fix, the call escaped the typed-error catches with exit 1 and
    empty stdout, violating the PRD JSON-contract guarantee.
    """
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        [
            "--format", "json", "--once",
            "--host", "bad\r\nval",
            "--token", "tok",
        ],
        client=None,  # let main() build Client(config) so InvalidURL fires
        stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_USAGE
    rendered = out.getvalue()
    parsed = json.loads(rendered)  # stdout MUST be parseable JSON
    assert parsed["error"]["code"] == "usage"
    assert "invalid --host" in parsed["error"]["message"]


def test_malformed_host_plain_format_still_typed_usage():
    """Same defense path under --format plain: exit 2, stderr carries
    the diagnostic, stdout stays empty (no JSON envelope for plain).
    """
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        [
            "--format", "plain", "--once",
            "--host", "bad\r\nval",
            "--token", "tok",
        ],
        client=None, stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_USAGE
    assert out.getvalue() == ""
    assert "invalid --host" in err.getvalue()


def test_client_init_raises_config_error_on_invalid_url():
    """Direct unit test of the wrapper inside Client.__init__: a
    Config with a CRLF-injected host raises ConfigError (subclass of
    FjQueueError), NOT httpx.InvalidURL.
    """
    bad = fq.Config(host="bad\r\nval", token="tok")
    with pytest.raises(fq.ConfigError, match="invalid --host"):
        fq.Client(bad)


# ---------------------------------------------------------------------------
# Runner metrics wiring through the CLI (--no-metrics disabled path +
# enabled path with a respx-mocked Prometheus).
# ---------------------------------------------------------------------------


def _prom_handler():
    fixture = json.loads((FIXTURES / "prometheus-live.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        if "container_cpu_usage_seconds_total" in q:
            return httpx.Response(200, json=fixture["cpu"])
        if "container_memory_working_set_bytes" in q:
            return httpx.Response(200, json=fixture["memory"])
        if "kube_pod_container_resource_limits" in q:
            if 'resource="memory"' in q:
                return httpx.Response(200, json=fixture["memory_limit"])
            return httpx.Response(200, json=fixture["cpu_limit"])
        if "kube_pod_info" in q:
            return httpx.Response(200, json=fixture["info"])
        # NCPS queries (job="ncps").
        if "request_duration_millis_milliseconds_count" in q:
            return httpx.Response(200, json=fixture["ncps_requests"])
        if "requests_inflight" in q:
            return httpx.Response(200, json=fixture["ncps_inflight"])
        if "http_client_request_duration_seconds_count" in q:
            return httpx.Response(200, json=fixture["ncps_upstream"])
        if "container_network_transmit_bytes_total" in q:
            return httpx.Response(200, json=fixture["ncps_bytes"])
        return httpx.Response(400, text="unexpected")

    return handler


def test_cli_no_metrics_sets_disabled_marker():
    """--no-metrics: runner_pods=[] and metrics.error="disabled", which
    is distinguishable from a successful fetch that found zero pods
    (metrics.error=null).
    """
    rc, out, err = _run(
        ["--format", "json", "--once", "--no-metrics"],
        client=_typical_client(),
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["runner_pods"] == []
    assert parsed["metrics"]["error"] == "disabled"
    assert parsed["metrics"]["source"] == "prometheus"


@respx.mock
def test_cli_metrics_enabled_populates_runner_pods():
    """--metrics opt-in path: with a reachable (mocked) Prometheus the
    JSON carries populated runner_pods with raw usage + limit numbers.
    """
    respx.get(
        url__regex=r"https://prometheus\.example\.com/api/v1/query.*"
    ).mock(side_effect=_prom_handler())

    rc, out, err = _run(
        ["--format", "json", "--once", "--metrics",
         "--metrics-url", "https://prometheus.example.com"],
        client=_typical_client(),
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["metrics"]["error"] is None
    pods = {p["pod"]: p for p in parsed["runner_pods"]}
    assert len(pods) == 3
    qf = pods["ci-runner-aaaa1111ff-pod1"]
    assert qf["memory_bytes"] == 763322368
    assert qf["memory_limit_bytes"] == 18674094196
    assert qf["cpu_limit_cores"] is None
    # NCPS is independent of metrics; without --ncps it stays disabled.
    assert parsed["ncps"] is None
    assert parsed["ncps_error"] == "disabled"


@respx.mock
def test_cli_metrics_and_ncps_both_enabled():
    """--metrics --ncps: both fetch from the same mocked Prometheus."""
    respx.get(
        url__regex=r"https://prometheus\.example\.com/api/v1/query.*"
    ).mock(side_effect=_prom_handler())

    rc, out, err = _run(
        ["--format", "json", "--once", "--metrics", "--ncps",
         "--metrics-url", "https://prometheus.example.com"],
        client=_typical_client(),
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["metrics"]["error"] is None
    assert len(parsed["runner_pods"]) == 3
    # NCPS also populated.
    assert parsed["ncps"] is not None
    assert parsed["ncps"]["active"] is True
    assert parsed["ncps"]["requests_per_sec"] == 8.5
    assert parsed["ncps_error"] is None


def test_cli_no_metrics_sets_ncps_disabled():
    """--no-metrics: ncps is null and ncps_error is 'disabled' (both off)."""
    rc, out, err = _run(
        ["--format", "json", "--once", "--no-metrics"],
        client=_typical_client(),
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["ncps"] is None
    assert parsed["ncps_error"] == "disabled"


@respx.mock
def test_cli_metrics_failure_degrades_gracefully():
    """A Prometheus 500 does not break the dashboard: exit 0, runner_pods
    empty, metrics.error carries the reason (not "disabled").
    """
    respx.get(
        url__regex=r"https://prometheus\.example\.com/api/v1/query.*"
    ).mock(return_value=httpx.Response(500, text="boom"))

    rc, out, err = _run(
        ["--format", "json", "--once", "--metrics",
         "--metrics-url", "https://prometheus.example.com"],
        client=_typical_client(),
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out)
    assert parsed["runner_pods"] == []
    assert parsed["metrics"]["error"] is not None
    assert parsed["metrics"]["error"] != "disabled"
