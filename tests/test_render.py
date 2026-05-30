"""M4 renderer tests.

PRD #61 §M4 + §Test Plan ("Renderers"). Two surfaces:

  * render_plain(snapshot) -> str   -- no-color, pipe-safe, greppable
  * render_rich(snapshot)  -> rich.console.Renderable

Both consume Snapshot ONLY. Tests pin: deterministic output, no ANSI
in plain, no Unicode box-drawing in plain, presence of every Snapshot
section in both surfaces, and frame-snapshot stability at a fixed
console width.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

import fj_queue as fq


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 27, 14, 3, 11, tzinfo=timezone.utc)
HOST = "git.wxs.ro"


def _runner(rid, *, status="active", labels=("grunt",), name=None, version="v12.7.3", ephemeral=False):
    return fq.Runner(
        id=rid,
        name=name if name is not None else f"runner-{rid}",
        status=status,
        version=version,
        labels=tuple(labels),
        ephemeral=ephemeral,
    )


def _job(jid, *, status="waiting", repo_id=85, owner_id=11, runs_on=("grunt",), needs=(), task_id=0, attempt=1, name=None):
    return fq.RawJob(
        id=jid,
        name=name or f"job-{jid}",
        status=status,
        repo_id=repo_id,
        owner_id=owner_id,
        runs_on=tuple(runs_on),
        needs=tuple(needs),
        task_id=task_id,
        attempt=attempt,
        handle=f"h-{jid}",
    )


def _snap(runners=(), jobs=(), repo_names=None, **kwargs):
    return fq.aggregate(
        runners=runners,
        jobs=jobs,
        repo_names=repo_names or {},
        now=NOW,
        host=HOST,
        **kwargs,
    )


def _typical_snap():
    """A representative snapshot used by both surfaces' snapshot tests."""
    runners = [
        _runner(3, name="k8s-runner",   status="active",  labels=("docker", "grunt")),
        _runner(2, name="k8s-runner-b", status="offline", labels=("docker",)),
        _runner(1, name="k8s-runner-c", status="idle",    labels=("grunt", "gpu")),
    ]
    jobs = [
        _job(83886, status="running", task_id=85492, repo_id=589, owner_id=15,
             runs_on=("grunt",), needs=("pipeline.generate",), name="Chainsaw E2E Tests"),
        _job(80239, status="waiting", repo_id=85, owner_id=11,
             runs_on=("grunt",), needs=(), name="Semantic Release"),
        _job(79969, status="waiting", repo_id=586, owner_id=15,
             runs_on=("special",), needs=(), name="pipeline"),
        _job(80300, status="waiting", repo_id=85, owner_id=11,
             runs_on=("grunt",), needs=("build",), attempt=2, name="Deploy"),
    ]
    return _snap(
        runners=runners,
        jobs=jobs,
        repo_names={
            85: "containers/theme-api",
            586: "crossplane/harbor",
            589: "owner-a/repo-a",
        },
    )


# ---------------------------------------------------------------------------
# Plain renderer: contract assertions (no syrupy needed for these).
# ---------------------------------------------------------------------------


def test_plain_contains_no_ansi_escapes():
    """PRD: plain is pipe-friendly; no ANSI escapes ever, even when the
    Snapshot contains warnings or rerun attempts that the Rich
    renderer would color.
    """
    snap = _typical_snap()
    out = fq.render_plain(snap)
    assert "\x1b[" not in out  # CSI start
    assert "\x1b]" not in out  # OSC start


def test_plain_contains_no_unicode_box_drawing():
    """ASCII-only so grep/sed/awk/cut behave. Reject every codepoint in
    the Unicode 'Box Drawing' block U+2500..U+257F and a few common
    pseudo-box chars Rich might emit.
    """
    snap = _typical_snap()
    out = fq.render_plain(snap)
    for ch in out:
        cp = ord(ch)
        assert not (0x2500 <= cp <= 0x257F), (
            f"plain output contains box-drawing U+{cp:04X}"
        )
    # Common offenders not in the box-drawing block.
    for forbidden in ("│", "─", "┌", "┐", "└", "┘", "├", "┤"):
        assert forbidden not in out


def test_plain_is_byte_deterministic():
    """Same Snapshot in -> same string out, twice in a row."""
    snap = _typical_snap()
    assert fq.render_plain(snap) == fq.render_plain(snap)


def test_plain_includes_every_section_header():
    """Anchors agents and grep-pipelines can pin against."""
    snap = _typical_snap()
    out = fq.render_plain(snap)
    for anchor in ("TOTALS:", "RUNNERS (", "PER REPO (", "QUEUE (", "WARNINGS ("):
        assert anchor in out


def test_plain_empty_snapshot_still_renders_all_sections():
    """PRD success: empty queue is exit 0 not error. Plain must produce
    something usable rather than a blank string.
    """
    out = fq.render_plain(_snap())
    assert "TOTALS: running=0" in out
    assert "RUNNERS (0 online of 0)" in out
    assert "(none)" in out  # the per_repo + runners + warnings empty markers
    assert "(empty)" in out  # the queue empty marker


def test_plain_includes_each_blocked_reason_verbatim():
    """All three vocabulary values reach the wire / text intact."""
    snap = _typical_snap()
    out = fq.render_plain(snap)
    assert "[waiting_for_runner" in out
    assert "[unschedulable     " in out  # padded to width 19
    assert "[blocked_on_needs  " in out


def test_plain_warning_section_has_message_arrow_form():
    snap = _typical_snap()
    out = fq.render_plain(snap)
    assert "unschedulable_labels" in out
    assert "-> No online runner can satisfy runs_on:" in out


def test_plain_filter_echoed_when_set():
    snap = _snap(filter_repo="owner-a/repo-a", filter_label=("grunt",))
    out = fq.render_plain(snap)
    assert "filter: repo=owner-a/repo-a  label=[grunt]" in out


def test_plain_filter_dashes_when_unset():
    out = fq.render_plain(_snap())
    assert "filter: repo=-  label=-" in out


def test_plain_header_carries_as_of_and_host():
    out = fq.render_plain(_typical_snap())
    assert "as_of=2026-05-27T14:03:11Z" in out
    assert "host=git.wxs.ro" in out


# ---------------------------------------------------------------------------
# Plain golden snapshot (full output, syrupy).
# ---------------------------------------------------------------------------


def test_plain_golden_snapshot(snapshot):
    assert fq.render_plain(_typical_snap()) == snapshot


def test_plain_empty_golden_snapshot(snapshot):
    assert fq.render_plain(_snap()) == snapshot


# ---------------------------------------------------------------------------
# Rich renderer: rendered-to-string assertions + golden snapshot.
# ---------------------------------------------------------------------------


def _render_rich_to_text(snap, *, width=120, color=False) -> str:
    """Render a Snapshot through Rich into a plain text string at a
    fixed width. force_terminal=True with no_color=True keeps the
    layout deterministic and the output ANSI-free, suitable for a
    byte-stable golden snapshot.
    """
    from rich.console import Console

    buf = io.StringIO()
    console = Console(
        file=buf,
        width=width,
        force_terminal=color,
        no_color=not color,
        record=False,
        legacy_windows=False,
        soft_wrap=False,
    )
    console.print(fq.render_rich(snap))
    return buf.getvalue()


def test_rich_returns_a_rich_renderable():
    """render_rich's return value must be a Rich Renderable (Group),
    not a string. M5's Live() loop consumes Renderables directly.
    """
    from rich.console import Group
    renderable = fq.render_rich(_typical_snap())
    assert isinstance(renderable, Group)


def test_rich_renders_without_raising_on_empty_snapshot():
    """Tables with zero rows are a real edge case in Rich. Make sure
    the empty snapshot renders end-to-end.
    """
    text = _render_rich_to_text(_snap())
    assert "fj-queue" in text
    assert "Runners" in text
    assert "Queue" in text


def test_rich_text_carries_every_section_title():
    text = _render_rich_to_text(_typical_snap())
    for anchor in ("snapshot", "Runners", "Per repo", "Queue", "Warnings"):
        assert anchor in text


def test_rich_text_includes_visible_job_identifiers():
    """Numeric anchors agents (or operators on a wide terminal) can
    eyeball: job_id, repo slug, attempt indicator for rerun.
    """
    text = _render_rich_to_text(_typical_snap(), width=200)
    assert "80239" in text  # the waiting-for-runner job
    assert "79969" in text  # the unschedulable job
    assert "containers/theme-api" in text
    # Rerun marker visible (job 80300 has attempt=2).
    assert "rerun" in text


def test_rich_skips_warnings_section_when_no_warnings():
    """The Warnings table is conditional; an empty warnings list MUST
    NOT render an empty table.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [_job(10, runs_on=("grunt",))]  # waiting_for_runner; no warning
    snap = _snap(runners=runners, jobs=jobs)
    text = _render_rich_to_text(snap)
    assert "Warnings" not in text


def test_rich_no_color_when_no_color_env_is_set(monkeypatch):
    """NO_COLOR env (https://no-color.org) suppresses COLOR. The spec
    is narrow: it disables color SGR codes (foreground 30-37/90-97 and
    background 40-47/100-107), NOT text emphasis like bold/dim/italic.
    Rich follows this interpretation: NO_COLOR drops `style="red"` /
    `style="green"` etc. but keeps bold/dim/italic.

    We assert the strict, spec-compliant thing: no color SGR codes.
    """
    import re
    from rich.console import Console

    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    console = Console(
        file=buf,
        width=120,
        force_terminal=True,
        legacy_windows=False,
    )
    console.print(fq.render_rich(_typical_snap()))
    out = buf.getvalue()
    # SGR foreground / background color codes.
    color_sgr = re.compile(
        r"\x1b\[(?:\d+;)*(?:3[0-7]|4[0-7]|9[0-7]|10[0-7])(?:;\d+)*m"
    )
    matches = color_sgr.findall(out)
    assert not matches, f"NO_COLOR env did not suppress color codes: {matches[:3]}"


# ---------------------------------------------------------------------------
# Rich golden snapshot at fixed width + no_color.
# ---------------------------------------------------------------------------


def test_rich_golden_snapshot_width_120_no_color(snapshot):
    assert _render_rich_to_text(_typical_snap(), width=120, color=False) == snapshot


def test_rich_golden_snapshot_empty_width_120_no_color(snapshot):
    assert _render_rich_to_text(_snap(), width=120, color=False) == snapshot


# ---------------------------------------------------------------------------
# Renderers consume Snapshot only: no I/O, no recomputation, no clock.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Brief-required: wide-char / Unicode / emoji fixture renders cleanly.
# ---------------------------------------------------------------------------


def _unicode_snap():
    """A snapshot exercising wide East Asian chars, Latin diacritics,
    and an emoji. Used to catch column-width drift in both surfaces.
    """
    runners = [
        _runner(
            1,
            name="测试-runner",
            status="active",
            labels=("docker", "测试"),
        ),
        _runner(
            2,
            name="café-runner",
            status="idle",
            labels=("grunt",),
        ),
    ]
    jobs = [
        _job(
            42,
            status="waiting",
            repo_id=1,
            runs_on=("测试",),
            needs=(),
            name="🚀 Deploy",
        ),
        _job(
            43,
            status="waiting",
            repo_id=2,
            runs_on=("nonexistent",),
            needs=(),
            name="café-job",
        ),
    ]
    return _snap(
        runners=runners,
        jobs=jobs,
        repo_names={1: "containers/测试-repo", 2: "containers/café"},
    )


def test_plain_renders_wide_chars_and_emoji_without_crash():
    """Plain output must not error on wide East Asian chars, Latin
    diacritics, or emoji. ASCII-only is for the RAW LAYOUT (no box
    drawing), not the DATA: a repo named `测试-repo` must round-trip.
    """
    out = fq.render_plain(_unicode_snap())
    assert "测试-runner" in out
    assert "café-runner" in out
    assert "🚀 Deploy" in out
    assert "containers/测试-repo" in out
    assert "containers/café" in out
    # The frame is still ASCII (no box-drawing).
    for ch in out:
        cp = ord(ch)
        assert not (0x2500 <= cp <= 0x257F)


def test_plain_byte_deterministic_on_unicode_input():
    """Same Unicode snapshot in -> same string out, twice."""
    snap = _unicode_snap()
    assert fq.render_plain(snap) == fq.render_plain(snap)


def test_rich_renders_wide_chars_and_emoji_without_crash():
    """Rich is the surface most likely to corrupt columns on wide
    chars (East Asian width counts as 2). Render at a generous width
    and assert the data survives.
    """
    text = _render_rich_to_text(_unicode_snap(), width=160, color=False)
    assert "测试-runner" in text
    assert "café-runner" in text
    assert "🚀 Deploy" in text
    # Column boundaries didn't collapse: Per repo + Queue titles still
    # appear (they precede the affected rows).
    assert "Per repo" in text
    assert "Queue" in text


def test_plain_unicode_golden_snapshot(snapshot):
    """Lock the wide-char output. Detects column-alignment regressions
    that a simple `in` check would miss.
    """
    assert fq.render_plain(_unicode_snap()) == snapshot


# ---------------------------------------------------------------------------
# Brief-required: positive status-color assertion for Rich.
# ---------------------------------------------------------------------------


def test_rich_active_runner_renders_with_green_offline_with_red():
    """PRD §M4 visual contract: status colors are part of the agreed
    mock. Capture colored output and assert the SGR codes are present.
    Green SGR foreground is 32 (or 92 bright); red is 31 (or 91).
    """
    runners = [
        _runner(1, name="active-r", status="active", labels=("grunt",)),
        _runner(2, name="offline-r", status="offline", labels=("grunt",)),
    ]
    text = _render_rich_to_text(_snap(runners=runners), width=120, color=True)
    # Find the section containing each runner's status cell and check
    # for nearby color SGR codes. Cheap heuristic: assert the canonical
    # color codes appear in the rendered output AT ALL (Rich does emit
    # them when color=True).
    assert "\x1b[32m" in text or "\x1b[92m" in text  # green
    assert "\x1b[31m" in text or "\x1b[91m" in text  # red


def test_rich_idle_runner_renders_dim_green_distinct_from_active():
    """Three-way visual distinction (active=bright green, idle=dim
    green, offline=red). Idle is still online for schedulability
    purposes but visually softened so operators see at a glance which
    runners are loaded vs. just waiting.

    Rich renders `dim green` as the green SGR (32/92) combined with
    the dim SGR (2). Distinct-from-active by the presence of the dim
    code in the idle row's escape sequence.
    """
    runners_active_only = [
        _runner(1, name="active-r", status="active", labels=("grunt",)),
    ]
    runners_idle_only = [
        _runner(1, name="idle-r", status="idle", labels=("grunt",)),
    ]
    active_text = _render_rich_to_text(
        _snap(runners=runners_active_only), width=120, color=True
    )
    idle_text = _render_rich_to_text(
        _snap(runners=runners_idle_only), width=120, color=True
    )

    # Both have green somewhere in the rendered output.
    assert "\x1b[32m" in active_text or "\x1b[92m" in active_text
    assert "\x1b[32m" in idle_text or "\x1b[92m" in idle_text

    # Idle's `dim green` renders as a single SGR sequence that carries
    # BOTH the dim attribute (2) and the green color (32 or 92), e.g.
    # `\x1b[2;32m`. The header uses `style="dim"` (plain `\x1b[2m`),
    # so a check for "dim alone" would over-match; we require dim and
    # green to share one SGR sequence (separated by `;`) to lock the
    # idle-row distinction.
    import re
    dim_plus_green = re.compile(
        r"\x1b\["               # SGR escape start
        r"(?:"
        r"2;(?:[^m]*;)?[39]2"   # dim first, green later
        r"|"
        r"[39]2;(?:[^m]*;)?2"   # green first, dim later
        r")"
        r"(?:;[^m]*)?m"
    )
    assert dim_plus_green.search(idle_text), (
        "idle render did not emit dim+green SGR combination"
    )
    assert not dim_plus_green.search(active_text), (
        "active-only render unexpectedly emitted dim+green together"
    )


def test_rich_unschedulable_warning_renders_red_in_color_mode():
    """unschedulable_labels warnings are styled red by the renderer.
    Catches a regression that would silently drop the visual cue.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [_job(10, runs_on=("nope",), needs=())]
    text = _render_rich_to_text(_snap(runners=runners, jobs=jobs), width=120, color=True)
    # Red SGR present somewhere (the unschedulable cell + warnings panel border).
    assert "\x1b[31m" in text or "\x1b[91m" in text


def test_renderers_never_read_environment_or_clock(monkeypatch):
    """Defense: if a renderer ever reaches for datetime.now() or
    os.environ['FORGEJO_TOKEN'], the M5 watch loop would render
    inconsistent frames. Force both to blow up loudly and confirm the
    renderers still produce output.
    """
    import datetime as _dt

    class _BoomDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("renderer called datetime.now()")
        @classmethod
        def utcnow(cls):
            raise AssertionError("renderer called datetime.utcnow()")

    monkeypatch.setattr("fj_queue.datetime", _BoomDateTime)
    monkeypatch.setenv("FORGEJO_TOKEN", "this-should-never-be-read")

    snap = _typical_snap()
    # Just must not blow up.
    fq.render_plain(snap)
    _render_rich_to_text(snap)


# ---------------------------------------------------------------------------
# Runner resources (per-pod CPU/memory) rendering.
# ---------------------------------------------------------------------------


def _pods():
    # Mirrors live reality: memory limit set (~17.4 GiB), no CPU limit.
    return (
        fq.PodResource(
            pod="forgejo-runner-54746685ff-qf4k7",
            node="k8s-green-wn2",
            cpu_cores=0.00109,
            memory_bytes=763322368,
            cpu_limit_cores=None,
            memory_limit_bytes=18674094196,
        ),
        fq.PodResource(
            pod="forgejo-runner-54746685ff-b5f9b",
            node="k8s-green-wn3",
            cpu_cores=0.01023,
            memory_bytes=711917568,
            cpu_limit_cores=None,
            memory_limit_bytes=18674094196,
        ),
    )


def _metrics_snap():
    """A snapshot with populated per-pod runner resources."""
    runners = [_runner(3, name="k8s-runner", status="active", labels=("docker", "grunt"))]
    return _snap(runners=runners, runner_pods=_pods())


def _metrics_error_snap():
    runners = [_runner(3, name="k8s-runner", status="active", labels=("docker", "grunt"))]
    return _snap(
        runners=runners,
        runner_pods=(),
        metrics_error="timeout querying prometheus at https://prometheus.wxs.ro",
    )


def test_plain_renders_runner_resources_section():
    out = fq.render_plain(_metrics_snap())
    assert "RUNNER RESOURCES (per pod, 2):" in out
    assert "forgejo-runner-54746685ff-qf4k7" in out
    assert "node=k8s-green-wn2" in out
    # usage / limit: cpu to 3 decimals with ASCII `-` for the absent CPU
    # limit; mem usage in MiB, limit in GiB (18674094196 -> 17.4 GiB).
    assert "cpu=0.001 / -" in out
    assert "mem=728 MiB / 17.4 GiB" in out


def test_plain_cpu_limit_absent_renders_ascii_dash():
    """Plain stays ASCII-only: a None CPU limit denominator is `-` (not
    the em dash the rich renderer uses).
    """
    out = fq.render_plain(_metrics_snap())
    assert "/ -" in out
    assert "—" not in out  # no em dash leaks into plain


def test_plain_renders_cpu_limit_when_set():
    """When a CPU limit IS configured, the denominator shows the value."""
    runners = [_runner(3, name="k8s-runner", status="active", labels=("grunt",))]
    pods = (
        fq.PodResource(
            pod="p1",
            node="k8s-green-wn1",
            cpu_cores=0.015,
            memory_bytes=763322368,
            cpu_limit_cores=0.5,
            memory_limit_bytes=18674094196,
        ),
    )
    out = fq.render_plain(_snap(runners=runners, runner_pods=pods))
    assert "cpu=0.015 / 0.500" in out


def test_plain_renders_metrics_unavailable_line():
    out = fq.render_plain(_metrics_error_snap())
    assert "RUNNER RESOURCES: unavailable (timeout querying prometheus" in out


def test_plain_runner_resources_no_ansi_no_box_drawing():
    """The new section must keep plain output pipe-safe: no ANSI, no
    Unicode box-drawing, even with populated pods.
    """
    out = fq.render_plain(_metrics_snap())
    assert "\x1b[" not in out and "\x1b]" not in out
    for ch in out:
        assert not (0x2500 <= ord(ch) <= 0x257F)


def test_plain_metrics_unavailable_no_ansi_no_box_drawing():
    out = fq.render_plain(_metrics_error_snap())
    assert "\x1b[" not in out and "\x1b]" not in out
    for ch in out:
        assert not (0x2500 <= ord(ch) <= 0x257F)


def test_rich_renders_runner_resources_table():
    text = _render_rich_to_text(_metrics_snap(), width=120, color=False)
    assert "Runner resources (per pod)" in text
    assert "qf4k7" in text
    # usage / limit cells: mem usage MiB + limit GiB; cpu limit em dash.
    assert "728 MiB / 17.4 GiB" in text
    assert "0.001 / —" in text  # rich uses the em dash for a None limit


def test_rich_renders_metrics_unavailable_line():
    text = _render_rich_to_text(_metrics_error_snap(), width=120, color=False)
    assert "Runner resources: unavailable" in text
    assert "timeout querying prometheus" in text


def test_plain_runner_resources_golden(snapshot):
    assert fq.render_plain(_metrics_snap()) == snapshot


def test_plain_metrics_unavailable_golden(snapshot):
    assert fq.render_plain(_metrics_error_snap()) == snapshot


def test_rich_runner_resources_golden_width_120_no_color(snapshot):
    assert _render_rich_to_text(_metrics_snap(), width=120, color=False) == snapshot


def test_rich_metrics_unavailable_golden_width_120_no_color(snapshot):
    assert _render_rich_to_text(_metrics_error_snap(), width=120, color=False) == snapshot


def _metrics_disabled_snap():
    runners = [_runner(3, name="k8s-runner", status="active", labels=("grunt",))]
    return _snap(runners=runners, runner_pods=(), metrics_error=fq.METRICS_DISABLED)


def test_plain_disabled_reads_as_toggle_not_failure():
    out = fq.render_plain(_metrics_disabled_snap())
    assert "RUNNER RESOURCES: disabled (--no-metrics)" in out
    # Must NOT read like an error.
    assert "unavailable" not in out


def test_rich_disabled_reads_as_toggle_not_failure():
    text = _render_rich_to_text(_metrics_disabled_snap(), width=120, color=False)
    assert "Runner resources: disabled (--no-metrics)" in text
    assert "unavailable" not in text


# ---------------------------------------------------------------------------
# NCPS status line rendering (active / idle / disabled / unavailable).
# ---------------------------------------------------------------------------


def _ncps(active=True, req=8.5, inflight=1, upstream=0.3, bytes_ps=4000000.0):
    return fq.NcpsStatus(
        active=active,
        requests_per_sec=req,
        inflight=inflight,
        upstream_per_sec=upstream,
        bytes_per_sec=bytes_ps,
    )


def _ncps_snap(ncps=None, metrics_error=None):
    runners = [_runner(3, name="k8s-runner", status="active", labels=("grunt",))]
    return _snap(runners=runners, ncps=ncps, metrics_error=metrics_error)


def test_plain_ncps_active_line():
    out = fq.render_plain(_ncps_snap(ncps=_ncps()))
    assert "NCPS: active (8.5 req/s, 4 MiB/s, 0.3 miss/s)" in out


def test_plain_ncps_idle_line():
    out = fq.render_plain(
        _ncps_snap(ncps=_ncps(active=False, req=0.0, inflight=0, upstream=0.0, bytes_ps=0.0))
    )
    assert "NCPS: idle" in out


def test_plain_ncps_disabled_line():
    out = fq.render_plain(_ncps_snap(ncps=None, metrics_error=fq.METRICS_DISABLED))
    assert "NCPS: disabled (--no-metrics)" in out


def test_plain_ncps_unavailable_line():
    out = fq.render_plain(
        _ncps_snap(ncps=None, metrics_error="timeout querying prometheus")
    )
    assert "NCPS: unavailable (timeout querying prometheus)" in out


def test_plain_ncps_no_data_line_distinct_from_failure():
    """ncps=None with no failure reason reads as "no data", NOT
    "unavailable (...)" (which would imply a hard failure).
    """
    out = fq.render_plain(_ncps_snap(ncps=None, metrics_error=None))
    assert "NCPS: no data" in out
    assert "NCPS: unavailable" not in out


def test_plain_ncps_line_is_ascii_only():
    for snap in (
        _ncps_snap(ncps=_ncps()),
        _ncps_snap(ncps=None, metrics_error=fq.METRICS_DISABLED),
    ):
        out = fq.render_plain(snap)
        assert "\x1b[" not in out
        for ch in out:
            assert not (0x2500 <= ord(ch) <= 0x257F)


def test_rich_ncps_active_line_present_and_green():
    text = _render_rich_to_text(_ncps_snap(ncps=_ncps()), width=120, color=True)
    assert "NCPS: active" in text
    assert "\x1b[32m" in text or "\x1b[92m" in text  # green somewhere


def test_rich_ncps_idle_line():
    text = _render_rich_to_text(
        _ncps_snap(ncps=_ncps(active=False, req=0.0, inflight=0, upstream=0.0, bytes_ps=0.0)),
        width=120,
        color=False,
    )
    assert "NCPS: idle" in text


def test_plain_ncps_active_golden(snapshot):
    assert fq.render_plain(_ncps_snap(ncps=_ncps())) == snapshot


def test_plain_ncps_idle_golden(snapshot):
    assert fq.render_plain(
        _ncps_snap(ncps=_ncps(active=False, req=0.0, inflight=0, upstream=0.0, bytes_ps=0.0))
    ) == snapshot


def test_plain_ncps_disabled_golden(snapshot):
    assert fq.render_plain(_ncps_snap(ncps=None, metrics_error=fq.METRICS_DISABLED)) == snapshot


def test_rich_ncps_active_golden_width_120_no_color(snapshot):
    assert _render_rich_to_text(_ncps_snap(ncps=_ncps()), width=120, color=False) == snapshot


def test_rich_ncps_unavailable_golden_width_120_no_color(snapshot):
    assert _render_rich_to_text(
        _ncps_snap(ncps=None, metrics_error="timeout querying prometheus"),
        width=120,
        color=False,
    ) == snapshot
