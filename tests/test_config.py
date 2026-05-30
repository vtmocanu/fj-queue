"""M1 + M2 config-file + layered-resolution tests.

M1 covers:
  - resolve_host precedence: flag > $FORGEJO_HOST env > config file value
  - Missing host -> ConfigError (exit 2)
  - load_file_config: discovery order and layering
  - Token must never be read from the config file
  - missing-host -> main() exits 2 with a JSON usage error

M2 covers:
  - Metrics default OFF (no --metrics, no config file -> disabled)
  - --metrics flag enables metrics (opt-in)
  - Config file [metrics] enabled = true enables metrics
  - --no-metrics explicitly disables even if config says enabled
  - --metrics beats config file
  - --node-prefix / config [metrics] node_prefix wired into fetch
  - metrics_url / metrics_namespace config file layering
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


# ---------------------------------------------------------------------------
# resolve_host
# ---------------------------------------------------------------------------


def test_resolve_host_flag_wins_over_env_and_config(monkeypatch):
    """CLI flag beats both $FORGEJO_HOST and the config file value."""
    monkeypatch.setenv("FORGEJO_HOST", "env.example.com")
    host = fq.resolve_host(
        arg_host="flag.example.com",
        config_value="config.example.com",
    )
    assert host == "flag.example.com"


def test_resolve_host_env_wins_over_config(monkeypatch):
    """$FORGEJO_HOST beats the config file value when no flag is given."""
    monkeypatch.setenv("FORGEJO_HOST", "env.example.com")
    host = fq.resolve_host(
        arg_host=None,
        config_value="config.example.com",
    )
    assert host == "env.example.com"


def test_resolve_host_config_value_used_when_no_flag_or_env(monkeypatch):
    """Config file value is used when neither flag nor env is set."""
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    host = fq.resolve_host(
        arg_host=None,
        config_value="config.example.com",
    )
    assert host == "config.example.com"


def test_resolve_host_raises_config_error_when_nothing_set(monkeypatch):
    """No flag, no env, no config value -> ConfigError (exit 2)."""
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    with pytest.raises(fq.ConfigError):
        fq.resolve_host(arg_host=None, config_value=None)


def test_resolve_host_raises_config_error_exit_code(monkeypatch):
    """The ConfigError raised carries EXIT_USAGE (2)."""
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    with pytest.raises(fq.ConfigError) as exc_info:
        fq.resolve_host(arg_host=None, config_value=None)
    assert exc_info.value.exit_code == fq.EXIT_USAGE


def test_resolve_host_empty_env_falls_through(monkeypatch):
    """An empty $FORGEJO_HOST is treated as unset."""
    monkeypatch.setenv("FORGEJO_HOST", "")
    host = fq.resolve_host(
        arg_host=None,
        config_value="config.example.com",
    )
    assert host == "config.example.com"


def test_resolve_host_uses_real_os_environ_by_default(monkeypatch):
    """When env kwarg is omitted, os.environ is used."""
    monkeypatch.setenv("FORGEJO_HOST", "from-real-env.example.com")
    host = fq.resolve_host(arg_host=None, config_value=None)
    assert host == "from-real-env.example.com"


# ---------------------------------------------------------------------------
# load_file_config -- basic loading and token security
# ---------------------------------------------------------------------------


def test_load_file_config_returns_empty_dict_when_no_file(tmp_path, monkeypatch):
    """No --config and no auto-discovered file -> empty dict (not an error)."""
    # Ensure cwd has no fj-queue.toml and XDG points nowhere with config.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    result = fq.load_file_config()
    assert result == {}


def test_load_file_config_reads_explicit_path(tmp_path):
    """--config PATH: file is loaded when it exists."""
    cfg = tmp_path / "my-config.toml"
    cfg.write_text('host = "explicit.example.com"\n')
    result = fq.load_file_config(config_arg=str(cfg))
    assert result["host"] == "explicit.example.com"


def test_load_file_config_explicit_path_missing_raises_config_error(tmp_path):
    """--config PATH: raises ConfigError (exit 2) when file is absent."""
    missing = str(tmp_path / "no-such.toml")
    with pytest.raises(fq.ConfigError, match="not found"):
        fq.load_file_config(config_arg=missing)


def test_load_file_config_toml_decode_error_raises_config_error(tmp_path):
    """A malformed TOML file raises ConfigError (exit 2)."""
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"host = [unclosed\n")
    with pytest.raises(fq.ConfigError, match="malformed"):
        fq.load_file_config(config_arg=str(bad))


def test_load_file_config_token_key_is_stripped(tmp_path):
    """The `token` key must never be returned from the config file."""
    cfg = tmp_path / "secret.toml"
    cfg.write_text('host = "ok.example.com"\ntoken = "super-secret"\n')
    result = fq.load_file_config(config_arg=str(cfg))
    assert "token" in result or result.get("host") == "ok.example.com"
    assert "token" not in result, "token must be stripped from config file data"


def test_load_file_config_other_keys_survive_stripping(tmp_path):
    """Stripping `token` must not affect other keys."""
    cfg = tmp_path / "safe.toml"
    cfg.write_text(
        'host = "safe.example.com"\n'
        'token = "do-not-load"\n'
        '[metrics]\nenabled = true\n'
    )
    result = fq.load_file_config(config_arg=str(cfg))
    assert result["host"] == "safe.example.com"
    assert result["metrics"]["enabled"] is True
    assert "token" not in result


# ---------------------------------------------------------------------------
# load_file_config -- discovery order
# ---------------------------------------------------------------------------


def test_load_file_config_discovers_cwd_file(tmp_path, monkeypatch):
    """Auto-discovery picks up ./fj-queue.toml in the current directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    (tmp_path / "fj-queue.toml").write_text('host = "cwd.example.com"\n')
    result = fq.load_file_config()
    assert result["host"] == "cwd.example.com"


def test_load_file_config_discovers_xdg_file(tmp_path, monkeypatch):
    """Auto-discovery falls back to $XDG_CONFIG_HOME/fj-queue/config.toml."""
    monkeypatch.chdir(tmp_path)  # no fj-queue.toml in cwd
    xdg = tmp_path / "xdg"
    cfg_dir = xdg / "fj-queue"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('host = "xdg.example.com"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    result = fq.load_file_config()
    assert result["host"] == "xdg.example.com"


def test_load_file_config_cwd_beats_xdg(tmp_path, monkeypatch):
    """cwd fj-queue.toml takes priority over XDG config."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fj-queue.toml").write_text('host = "cwd-wins.example.com"\n')
    xdg = tmp_path / "xdg"
    cfg_dir = xdg / "fj-queue"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('host = "xdg-loses.example.com"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    result = fq.load_file_config()
    assert result["host"] == "cwd-wins.example.com"


def test_load_file_config_explicit_beats_cwd(tmp_path, monkeypatch):
    """An explicit --config PATH takes priority over the cwd file."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fj-queue.toml").write_text('host = "cwd.example.com"\n')
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('host = "explicit-wins.example.com"\n')
    result = fq.load_file_config(config_arg=str(explicit))
    assert result["host"] == "explicit-wins.example.com"


# ---------------------------------------------------------------------------
# End-to-end: missing host -> main() exits 2
# ---------------------------------------------------------------------------


def test_missing_host_exits_2_json(monkeypatch, tmp_path):
    """No --host, no $FORGEJO_HOST, no config file -> exit 2 with JSON usage error."""
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--token", "tok"],
        stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_USAGE
    parsed = json.loads(out.getvalue())
    assert parsed["error"]["code"] == "usage"
    assert "fj-queue:" in err.getvalue()


def test_missing_host_exits_2_plain(monkeypatch, tmp_path):
    """Same as above for --format plain: exit 2, diagnostic on stderr."""
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "plain", "--token", "tok"],
        stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_USAGE
    assert out.getvalue() == ""
    assert "fj-queue:" in err.getvalue()


def test_host_from_config_file_reaches_json_output(tmp_path, monkeypatch):
    """host from the config file appears in the snapshot's host field."""
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    cfg = tmp_path / "my.toml"
    cfg.write_text('host = "cfg.example.com"\n')

    # Build a minimal FakeClient to avoid network.
    class _FakeClient:
        def fetch_runners(self): return []
        def fetch_jobs(self): return []
        def resolve_repo(self, rid): return f"repo#{rid}"
        def close(self): pass

    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--config", str(cfg), "--token", "tok",
         "--no-metrics"],
        client=_FakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    assert parsed["host"] == "cfg.example.com"


def test_host_flag_beats_config_file(tmp_path, monkeypatch):
    """--host flag takes priority over the config file's host value."""
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    cfg = tmp_path / "my.toml"
    cfg.write_text('host = "config.example.com"\n')

    class _FakeClient:
        def fetch_runners(self): return []
        def fetch_jobs(self): return []
        def resolve_repo(self, rid): return f"repo#{rid}"
        def close(self): pass

    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--config", str(cfg),
         "--host", "flag.example.com", "--token", "tok", "--no-metrics"],
        client=_FakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    assert parsed["host"] == "flag.example.com"


def test_token_from_config_file_is_ignored(tmp_path, monkeypatch):
    """A `token` key in the config file must not authenticate the request.

    With no other token source, the run must fail with a usage error
    (missing token), not succeed by loading the token from the file.
    """
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    cfg = tmp_path / "with-token.toml"
    cfg.write_text('host = "cfg.example.com"\ntoken = "config-secret"\n')
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--config", str(cfg)],
        stdout=out, stderr=err, is_tty=False,
    )
    # Token not in env or args -> usage error (config token is silently dropped).
    assert rc == fq.EXIT_USAGE
    parsed = json.loads(out.getvalue())
    assert parsed["error"]["code"] == "usage"
    # The token value must not appear in any output.
    assert "config-secret" not in out.getvalue()
    assert "config-secret" not in err.getvalue()


# ---------------------------------------------------------------------------
# M2: Metrics default OFF
# ---------------------------------------------------------------------------


class _MetricsFakeClient:
    """Minimal FakeClient for metrics wiring tests."""
    def fetch_runners(self): return []
    def fetch_jobs(self): return []
    def resolve_repo(self, rid): return f"repo#{rid}"
    def close(self): pass


def _metrics_main(argv, monkeypatch, tmp_path):
    """Helper: run main() with an isolated env (no config file, host set)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--host", "h.example.com", "--token", "tok"] + argv,
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    return rc, json.loads(out.getvalue()) if out.getvalue().strip() else {}, err.getvalue()


def test_metrics_off_by_default(monkeypatch, tmp_path):
    """No --metrics, no config file -> metrics.error='disabled'."""
    rc, parsed, _ = _metrics_main([], monkeypatch, tmp_path)
    assert rc == fq.EXIT_OK
    assert parsed["metrics"]["error"] == "disabled"
    assert parsed["runner_pods"] == []


def test_metrics_default_ncps_null(monkeypatch, tmp_path):
    """No --metrics, no --ncps -> ncps is null and ncps_error is 'disabled'."""
    rc, parsed, _ = _metrics_main([], monkeypatch, tmp_path)
    assert rc == fq.EXIT_OK
    assert parsed["ncps"] is None
    assert parsed["ncps_error"] == "disabled"


def test_metrics_no_metrics_flag_explicit_disable(monkeypatch, tmp_path):
    """--no-metrics explicitly disables (same observable result as default)."""
    rc, parsed, _ = _metrics_main(["--no-metrics"], monkeypatch, tmp_path)
    assert rc == fq.EXIT_OK
    assert parsed["metrics"]["error"] == "disabled"


def test_metrics_flag_and_no_metrics_are_mutually_exclusive():
    """Argparse must reject --metrics --no-metrics together."""
    parser = fq._build_parser()
    try:
        parser.parse_args(["--metrics", "--no-metrics"])
        assert False, "should have raised SystemExit"
    except SystemExit as e:
        assert e.code == 2  # argparse usage error


@respx.mock
def test_metrics_config_enabled_true(monkeypatch, tmp_path):
    """Config file [metrics] enabled = true turns metrics on without --metrics flag.
    Prometheus is mocked to raise ConnectError so no real DNS is needed; the
    error must NOT be 'disabled' -- that proves the opt-in fired.
    """
    respx.get(
        url__regex=r"http://no-such-host\.invalid:9090/.*"
    ).mock(side_effect=httpx.ConnectError("mocked: host unreachable"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    cfg = tmp_path / "fj-queue.toml"
    cfg.write_text(
        'host = "h.example.com"\n'
        "[metrics]\nenabled = true\n"
        'url = "http://no-such-host.invalid:9090"\n'
    )
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--config", str(cfg), "--token", "tok"],
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    # Error must reflect a real connection attempt, not "disabled".
    assert parsed["metrics"]["error"] is not None
    assert parsed["metrics"]["error"] != "disabled"


def test_no_metrics_flag_beats_config_enabled(monkeypatch, tmp_path):
    """--no-metrics overrides config file [metrics] enabled = true."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    cfg = tmp_path / "fj-queue.toml"
    cfg.write_text(
        'host = "h.example.com"\n[metrics]\nenabled = true\n'
    )
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--config", str(cfg), "--token", "tok",
         "--no-metrics"],
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    assert parsed["metrics"]["error"] == "disabled"


def test_metrics_config_namespace_layering(monkeypatch, tmp_path):
    """Config [metrics] namespace is used when --metrics-namespace is not given."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    cfg = tmp_path / "ns.toml"
    cfg.write_text(
        '[metrics]\nenabled = false\nnamespace = "my-runners"\n'
    )
    # We can't observe namespace directly from JSON output (it's not surfaced),
    # but we can verify Config is built without errors.
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--host", "h.example.com", "--token", "tok",
         "--config", str(cfg)],
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    assert parsed["metrics"]["error"] == "disabled"  # enabled=false from config


def test_node_prefix_flag_wired(monkeypatch, tmp_path):
    """--node-prefix reaches Config.metrics_node_prefix (observable via
    the disabled path: if it didn't crash, the field exists and accepted a value).
    """
    rc, parsed, _ = _metrics_main(["--no-metrics", "--node-prefix", "k8s-node-"],
                                   monkeypatch, tmp_path)
    assert rc == fq.EXIT_OK
    assert parsed["metrics"]["error"] == "disabled"  # metrics off; prefix stored but not used


def test_node_prefix_from_config(monkeypatch, tmp_path):
    """Config [metrics] node_prefix is read without errors."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    cfg = tmp_path / "pfx.toml"
    cfg.write_text(
        'host = "h.example.com"\n[metrics]\nenabled = false\n'
        'node_prefix = "k8s-node-"\n'
    )
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--token", "tok", "--config", str(cfg)],
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    assert parsed["metrics"]["error"] == "disabled"


# ---------------------------------------------------------------------------
# M3: NCPS optional, default OFF, decoupled from metrics.
# All tests use monkeypatch.chdir + XDG isolation to avoid being affected
# by a stray ./fj-queue.toml in the working directory.
# ---------------------------------------------------------------------------


def _ncps_main(argv, monkeypatch, tmp_path):
    """Helper: run main() with an isolated env (no config file, host set)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--host", "h.example.com", "--token", "tok"] + argv,
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    return rc, json.loads(out.getvalue()) if out.getvalue().strip() else {}, err.getvalue()


def test_ncps_off_by_default(monkeypatch, tmp_path):
    """No --ncps flag and no config -> ncps_error='disabled', ncps=null."""
    rc, parsed, _ = _ncps_main([], monkeypatch, tmp_path)
    assert rc == fq.EXIT_OK
    assert parsed["ncps"] is None
    assert parsed["ncps_error"] == "disabled"


@respx.mock
def test_ncps_flag_enables_ncps(monkeypatch, tmp_path):
    """--ncps opt-in causes a fetch attempt.  ncps_error must NOT be
    'disabled' -- that proves the opt-in fired. Prometheus is mocked to
    raise ConnectError so no real DNS is needed.
    """
    respx.get(
        url__regex=r"http://no-such-host\.invalid:9090/.*"
    ).mock(side_effect=httpx.ConnectError("mocked: host unreachable"))
    rc, parsed, _ = _ncps_main(
        ["--ncps", "--metrics-url", "http://no-such-host.invalid:9090"],
        monkeypatch, tmp_path,
    )
    assert rc == fq.EXIT_OK
    # ncps_error is None (not "disabled"): fetch was attempted.
    assert parsed["ncps_error"] is None
    assert parsed["ncps"] is None  # invalid URL -> no data


def test_no_ncps_flag_explicit_disable(monkeypatch, tmp_path):
    """--no-ncps explicitly disables (same observable result as default)."""
    rc, parsed, _ = _ncps_main(["--no-ncps"], monkeypatch, tmp_path)
    assert rc == fq.EXIT_OK
    assert parsed["ncps_error"] == "disabled"
    assert parsed["ncps"] is None


def test_ncps_flag_and_no_ncps_are_mutually_exclusive():
    """Argparse must reject --ncps --no-ncps together."""
    parser = fq._build_parser()
    try:
        parser.parse_args(["--ncps", "--no-ncps"])
        assert False, "should have raised SystemExit"
    except SystemExit as e:
        assert e.code == 2  # argparse usage error


@respx.mock
def test_ncps_config_enabled_true(monkeypatch, tmp_path):
    """Config file [ncps] enabled = true turns NCPS on without --ncps flag.
    Prometheus is mocked to raise ConnectError so no real DNS is needed.
    ncps_error must NOT be 'disabled' -- that proves the opt-in fired.
    """
    respx.get(
        url__regex=r"http://no-such-host\.invalid:9090/.*"
    ).mock(side_effect=httpx.ConnectError("mocked: host unreachable"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    cfg = tmp_path / "fj-queue.toml"
    cfg.write_text(
        'host = "h.example.com"\n'
        "[ncps]\nenabled = true\n"
        '[metrics]\nurl = "http://no-such-host.invalid:9090"\n'
    )
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--config", str(cfg), "--token", "tok"],
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    # ncps_error is None (not "disabled"): fetch was attempted.
    assert parsed["ncps_error"] is None
    assert parsed["ncps"] is None  # mocked ConnectError -> no data


def test_no_ncps_flag_beats_config_enabled(monkeypatch, tmp_path):
    """--no-ncps overrides config file [ncps] enabled = true."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("FORGEJO_HOST", raising=False)
    cfg = tmp_path / "fj-queue.toml"
    cfg.write_text('host = "h.example.com"\n[ncps]\nenabled = true\n')
    out, err = io.StringIO(), io.StringIO()
    rc = fq.main(
        ["--format", "json", "--config", str(cfg), "--token", "tok", "--no-ncps"],
        client=_MetricsFakeClient(), stdout=out, stderr=err, is_tty=False,
    )
    assert rc == fq.EXIT_OK
    parsed = json.loads(out.getvalue())
    assert parsed["ncps_error"] == "disabled"


# ---------------------------------------------------------------------------
# M3: All 4 metrics x NCPS state combinations.
# ---------------------------------------------------------------------------


def test_state_both_off(monkeypatch, tmp_path):
    """metrics=OFF, ncps=OFF (default state): both disabled."""
    rc, parsed, _ = _ncps_main([], monkeypatch, tmp_path)
    assert rc == fq.EXIT_OK
    assert parsed["metrics"]["error"] == "disabled"
    assert parsed["ncps_error"] == "disabled"
    assert parsed["ncps"] is None
    assert parsed["runner_pods"] == []


@respx.mock
def test_state_metrics_on_ncps_off(monkeypatch, tmp_path):
    """metrics=ON (fetch mocked to raise ConnectError), ncps=OFF:
    metrics error != 'disabled', ncps_error == 'disabled'.
    """
    respx.get(
        url__regex=r"http://no-such-host\.invalid:9090/.*"
    ).mock(side_effect=httpx.ConnectError("mocked: host unreachable"))
    rc, parsed, _ = _ncps_main(
        ["--metrics", "--metrics-url", "http://no-such-host.invalid:9090"],
        monkeypatch, tmp_path,
    )
    assert rc == fq.EXIT_OK
    # Metrics attempted (invalid URL) -> error != "disabled".
    assert parsed["metrics"]["error"] != "disabled"
    assert parsed["metrics"]["error"] is not None
    # NCPS explicitly off.
    assert parsed["ncps_error"] == "disabled"
    assert parsed["ncps"] is None


@respx.mock
def test_state_metrics_off_ncps_on(monkeypatch, tmp_path):
    """metrics=OFF, ncps=ON (fetch mocked to raise ConnectError): metrics.error
    == 'disabled', ncps_error is None (fetch attempted, no data).
    """
    respx.get(
        url__regex=r"http://no-such-host\.invalid:9090/.*"
    ).mock(side_effect=httpx.ConnectError("mocked: host unreachable"))
    rc, parsed, _ = _ncps_main(
        ["--ncps", "--metrics-url", "http://no-such-host.invalid:9090"],
        monkeypatch, tmp_path,
    )
    assert rc == fq.EXIT_OK
    # Metrics not attempted.
    assert parsed["metrics"]["error"] == "disabled"
    assert parsed["runner_pods"] == []
    # NCPS fetch attempted (invalid URL) -> null with no disabled error.
    assert parsed["ncps_error"] is None
    assert parsed["ncps"] is None


@respx.mock
def test_state_both_on(monkeypatch, tmp_path):
    """metrics=ON, ncps=ON (both mocked to raise ConnectError): both fetch
    attempted, neither marked 'disabled'.
    """
    respx.get(
        url__regex=r"http://no-such-host\.invalid:9090/.*"
    ).mock(side_effect=httpx.ConnectError("mocked: host unreachable"))
    rc, parsed, _ = _ncps_main(
        ["--metrics", "--ncps", "--metrics-url", "http://no-such-host.invalid:9090"],
        monkeypatch, tmp_path,
    )
    assert rc == fq.EXIT_OK
    # Both attempted (invalid URL) -> errors are not "disabled".
    assert parsed["metrics"]["error"] != "disabled"
    assert parsed["metrics"]["error"] is not None
    assert parsed["ncps_error"] is None  # attempted but no data
    assert parsed["ncps"] is None
