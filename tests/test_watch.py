"""M5 live watch mode tests.

PRD #61 §M5 + §Test Plan ("Watch loop"). The PRD-mandated scripted
sequence is `[good, good, timeout, good]` -- four ticks that exercise:

  1. first good tick: render fresh, no stale marker, last-good cached.
  2. second good tick: same, last-good refreshed.
  3. timeout (ConnectionError): retry-in-place, last-good rendered
     with `STALE (last good HH:MM:SS)` marker, NO crash.
  4. recovery good tick: stale marker gone, fresh data again.

Plus: AuthError stops immediately (no retry on bad token),
first-tick ConnectionError exits (no empty dashboard), KeyboardInterrupt
exits cleanly, sleep is injected (no real sleep in tests).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Callable

import pytest

import fj_queue as fq


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

UTC = timezone.utc
T0 = datetime(2026, 5, 27, 14, 3, 11, tzinfo=UTC)
HOST = "git.example.com"


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
    """A scripted-source replacement for fj_queue.Client.

    Each entry in `script` is either:
      * a tuple `(runners, jobs, repo_names)` for a successful tick, or
      * an Exception INSTANCE to raise on that tick.

    fetch_runners pops one script entry; fetch_jobs returns the cached
    jobs from the same entry; resolve_repo returns from the cached map
    (with the standard `repo#<id>` fallback for misses). Tick counts
    are observable via `.tick_calls`.
    """

    def __init__(self, script: list):
        self._script = list(script)
        self._cursor = 0
        self._current_runners: list[fq.Runner] = []
        self._current_jobs: list[fq.RawJob] = []
        self._current_repos: dict[int, str] = {}
        self.tick_calls = 0
        self.close_calls = 0

    def __enter__(self):  # not used by run_watch (caller-owned client)
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        self.close_calls += 1

    def fetch_runners(self) -> list[fq.Runner]:
        if self._cursor >= len(self._script):
            raise IndexError("FakeClient script exhausted")
        entry = self._script[self._cursor]
        self._cursor += 1
        self.tick_calls += 1
        # BaseException covers KeyboardInterrupt + SystemExit too;
        # tests pass these to verify the loop's clean-exit handling.
        if isinstance(entry, BaseException):
            self._current_runners = []
            self._current_jobs = []
            self._current_repos = {}
            raise entry
        runners, jobs, repos = entry
        self._current_runners = list(runners)
        self._current_jobs = list(jobs)
        self._current_repos = dict(repos)
        return list(runners)

    def fetch_jobs(self) -> list[fq.RawJob]:
        return list(self._current_jobs)

    def resolve_repo(self, repo_id: int) -> str:
        return self._current_repos.get(repo_id, f"repo#{repo_id}")


def _good_tick(tag: str = "default"):
    """A small but realistic successful tick payload."""
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [_job(10, runs_on=("grunt",), needs=(), name=f"job-{tag}")]
    repos = {85: "owner-c/theme-api"}
    return (runners, jobs, repos)


def _silent_console():
    """A Rich Console that swallows output. Tests inspect via on_frame
    callbacks instead of parsing the rendered text.
    """
    from rich.console import Console
    return Console(file=io.StringIO(), force_terminal=False, width=120)


def _capturing_console():
    """Like _silent_console but returns (console, buf) so the caller
    can inspect the rendered output. Used by tests that assert on the
    one-frame AuthError error message.
    """
    from rich.console import Console
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=120), buf


def _stepping_clock(start: datetime, step_seconds: int = 1) -> Callable[[], datetime]:
    """Returns a clock callable that advances by `step_seconds` each
    invocation, so each tick has a distinguishable as_of.
    """
    state = {"t": start}

    def _now() -> datetime:
        current = state["t"]
        state["t"] = current.replace(second=current.second + step_seconds)
        return current

    return _now


# ---------------------------------------------------------------------------
# Happy path (brief: 3 distinct frames, no errors).
# ---------------------------------------------------------------------------


def test_happy_path_three_distinct_frames():
    """source yields [good, good, good]; loop renders 3 frames, each a
    distinct Snapshot, no stale markers, no errors.
    """
    client = FakeClient([_good_tick("a"), _good_tick("b"), _good_tick("c")])
    frames: list = []
    rc = fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=3,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=_silent_console(),
        on_frame=lambda i, s, m: frames.append((i, s, m)),
        screen=False,
    )
    assert rc == fq.EXIT_OK
    assert len(frames) == 3
    assert all(m is None for _, _, m in frames)
    # Each frame is a distinct Snapshot object (fresh fetch each tick).
    snaps = [s for _, s, _ in frames]
    assert snaps[0] is not snaps[1] is not snaps[2]
    # And the job name differs per tick (a/b/c), proving distinct data.
    names = [s.queue[0].job_name for s in snaps]
    assert names == ["job-a", "job-b", "job-c"]


# ---------------------------------------------------------------------------
# The canonical PRD scripted sequence.
# ---------------------------------------------------------------------------


def test_scripted_sequence_good_good_timeout_good():
    """The PRD-mandated `[good, good, timeout, good]`:
      tick 1: good   -> frame snapshot, no marker
      tick 2: good   -> frame snapshot (new), no marker
      tick 3: timeout-> frame is LAST-GOOD (tick 2) with stale marker
      tick 4: good   -> frame snapshot (new), marker cleared
    """
    client = FakeClient([
        _good_tick("a"),
        _good_tick("b"),
        fq.ConnectionError("timeout calling /api/v1/admin/actions/runners"),
        _good_tick("c"),
    ])
    frames: list[tuple[int, fq.Snapshot | None, str | None]] = []

    rc = fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=4,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=_silent_console(),
        on_frame=lambda i, s, m: frames.append((i, s, m)),
        screen=False,
    )

    assert rc == fq.EXIT_OK
    assert len(frames) == 4
    # Tick 1 + 2 + 4: fresh snapshots, marker None.
    assert frames[0][2] is None
    assert frames[1][2] is None
    assert frames[3][2] is None
    # Tick 3: stale, marker present, snapshot is tick-2's last-good.
    i3, snap3, marker3 = frames[2]
    assert i3 == 3
    assert marker3 is not None
    assert marker3.startswith("STALE (last good ")
    assert snap3 is frames[1][1]  # exact same Snapshot object as tick 2
    # Tick 4 recovers: snapshot is fresh, not the same object as tick 2.
    assert frames[3][1] is not frames[1][1]


def test_stale_marker_carries_last_good_at_hhmmss_and_interval():
    """`STALE (last good HH:MM:SS UTC, retrying every Ns)` is the
    exact format from PRD §M5 + expanded brief. The HH:MM:SS comes
    from the LAST-GOOD tick's clock (NOT the failing tick's), and
    the `Ns` carries the configured interval.
    """
    client = FakeClient([
        _good_tick(),
        fq.ConnectionError("timeout"),
    ])
    frames: list = []

    # Last-good fires at T0 = 14:03:11; failure at T0 + 1s.
    # interval=2.0 is the documented PRD default.
    fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=2.0,
        iterations=2,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=_silent_console(),
        on_frame=lambda i, s, m: frames.append((i, s, m)),
        screen=False,
    )

    assert frames[1][2] == "STALE (last good 14:03:11 UTC, retrying every 2s)"


def test_stale_marker_fractional_interval_formatted_with_g():
    """`{interval:g}s` keeps fractional values readable (`2.5s`)
    while trimming `.0` from whole-number floats (`2s`).
    """
    client = FakeClient([
        _good_tick(),
        fq.ConnectionError("timeout"),
    ])
    frames: list = []
    fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=2.5,
        iterations=2,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=_silent_console(),
        on_frame=lambda i, s, m: frames.append((i, s, m)),
        screen=False,
    )
    assert "retrying every 2.5s" in frames[1][2]


# ---------------------------------------------------------------------------
# Auth + schema drift: do NOT spin.
# ---------------------------------------------------------------------------


def test_auth_error_on_first_tick_raises_without_rendering_anything():
    """Bad token on tick 1: PRD says 'don't render an empty/error
    dashboard'. The exception bubbles before any live.update() runs;
    on_frame is never called.
    """
    client = FakeClient([fq.AuthError("401 unauthorized")])
    frames: list = []
    with pytest.raises(fq.AuthError):
        fq.run_watch(
            config=fq.Config(host=HOST, token="bad", timeout=5.0, metrics_enabled=False),
            interval=0.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=lambda _s: None,
            console=_silent_console(),
            on_frame=lambda i, s, m: frames.append((i, s, m)),
            screen=False,
        )
    assert client.tick_calls == 1
    # Critical: zero frames -- no empty dashboard, no error frame.
    assert frames == []


def test_auth_error_after_some_good_ticks_renders_error_frame_then_raises():
    """Mid-loop AuthError: render a ONE-FRAME error panel via Live,
    pause briefly so the operator sees it, then re-raise. The auth
    error frame's marker is the sentinel string 'AUTH_ERROR' on
    on_frame; the rendered text contains a human-readable error
    panel.
    """
    client = FakeClient([
        _good_tick(),
        _good_tick(),
        fq.AuthError("403 forbidden: admin scope required"),
    ])
    frames: list = []
    pause_calls: list[float] = []
    console, buf = _capturing_console()
    with pytest.raises(fq.AuthError):
        fq.run_watch(
            config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
            interval=0.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=pause_calls.append,
            console=console,
            on_frame=lambda i, s, m: frames.append((i, s, m)),
            screen=False,
            error_pause=0.5,
        )
    # Three frames recorded: two good snapshots + one auth-error
    # marker frame.
    assert len(frames) == 3
    assert frames[2] == (3, None, "AUTH_ERROR")
    # The error_pause is in the sleep call list (last call, since the
    # AuthError tick triggers the half-second pause before raising).
    assert 0.5 in pause_calls
    # The rendered output contains the user-visible error message.
    out = buf.getvalue()
    assert "auth error" in out.lower() or "Authentication failed" in out


def test_schema_drift_stops_no_retry():
    """SchemaDrift is DEFINITIVE (PRD §Risks line 182: 'fail loud,
    typed schema_drift, exit 5'). It is NOT transient: a Forgejo
    upgrade with a changed API shape, or a cross-host next-link
    security refusal, will not recover by retrying. The loop stops
    immediately. (Reverted from the brief's transient grouping; the
    PRD is canonical.)
    """
    client = FakeClient([
        _good_tick("a"),
        fq.SchemaDrift("unexpected payload shape"),
        _good_tick("b"),  # must never be reached
    ])
    frames: list = []
    with pytest.raises(fq.SchemaDrift):
        fq.run_watch(
            config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
            interval=2.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=lambda _s: None,
            console=_silent_console(),
            on_frame=lambda i, s, m: frames.append((i, s, m)),
            screen=False,
        )
    # Two frames: the good tick 1, then the mid-loop schema-drift error
    # frame. Tick 3 (the second good tick) is never reached.
    assert len(frames) == 2
    assert frames[0][2] is None              # tick 1 fresh, no marker
    assert frames[1] == (2, None, "SCHEMA_DRIFT")  # error frame
    assert client.tick_calls == 2


def test_mid_loop_schema_drift_renders_error_frame_then_raises():
    """Symmetry with mid-loop AuthError: a SchemaDrift after a good
    tick renders a red error panel ('schema drift: ...') via Live,
    pauses error_pause, then re-raises. NO STALE banner (it is not
    transient).
    """
    client = FakeClient([
        _good_tick(),
        fq.SchemaDrift("runners payload was an object, expected list"),
    ])
    frames: list = []
    pause_calls: list[float] = []
    console, buf = _capturing_console()
    with pytest.raises(fq.SchemaDrift):
        fq.run_watch(
            config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
            interval=0.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=pause_calls.append,
            console=console,
            on_frame=lambda i, s, m: frames.append((i, s, m)),
            screen=False,
            error_pause=0.5,
        )
    assert len(frames) == 2
    assert frames[1] == (2, None, "SCHEMA_DRIFT")
    assert 0.5 in pause_calls
    out = buf.getvalue()
    assert "schema drift" in out.lower()
    # NOT a stale banner.
    assert "STALE" not in out


def test_first_tick_schema_drift_raises_no_dashboard():
    """First-tick SchemaDrift has no last-good to render, so it
    bubbles. Same shape as first-tick ConnectionError; CLI maps to
    exit=5.
    """
    client = FakeClient([fq.SchemaDrift("unexpected payload shape")])
    frames: list = []
    with pytest.raises(fq.SchemaDrift):
        fq.run_watch(
            config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
            interval=0.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=lambda _s: None,
            console=_silent_console(),
            on_frame=lambda i, s, m: frames.append((i, s, m)),
            screen=False,
        )
    assert frames == []


# ---------------------------------------------------------------------------
# First-tick ConnectionError: exit rather than render empty dashboard.
# ---------------------------------------------------------------------------


def test_first_tick_connection_error_raises_no_dashboard():
    """PRD: 'first-tick failure exits rather than rendering empty
    dashboard'. We have no last-good to fall back to; raise so the CLI
    (M6) maps to exit=4. Assert NO frame was rendered.
    """
    client = FakeClient([fq.ConnectionError("connection refused")])
    frames: list = []
    with pytest.raises(fq.ConnectionError):
        fq.run_watch(
            config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
            interval=0.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=lambda _s: None,
            console=_silent_console(),
            on_frame=lambda i, s, m: frames.append((i, s, m)),
            screen=False,
        )
    assert frames == []


# ---------------------------------------------------------------------------
# Ctrl-C clean exit.
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_during_sleep_exits_130():
    """Operator hits Ctrl-C between ticks. Live's __exit__ cleans up
    the screen; we return 130 (POSIX 128 + SIGINT) so agents can
    distinguish operator-stop from normal completion.
    """
    client = FakeClient([_good_tick(), _good_tick()])

    def sleeper(_s):
        # Fire on the FIRST sleep call (after tick 1).
        raise KeyboardInterrupt()

    rc = fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=10,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=sleeper,
        console=_silent_console(),
        screen=False,
    )
    assert rc == 130 == fq.EXIT_INTERRUPTED
    # Tick 1 fired; sleep raised before tick 2.
    assert client.tick_calls == 1


def test_keyboard_interrupt_during_fetch_exits_130():
    """A Ctrl-C raised mid-fetch (e.g. inside httpx) also yields a
    clean 130 exit. KeyboardInterrupt isn't an FjQueueError, so it
    MUST NOT be classified as ConnectionError/AuthError/SchemaDrift;
    the `except KeyboardInterrupt` in run_watch catches it explicitly.
    """
    client = FakeClient([KeyboardInterrupt()])
    rc = fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=10,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=_silent_console(),
        screen=False,
    )
    assert rc == 130 == fq.EXIT_INTERRUPTED


# ---------------------------------------------------------------------------
# Iterations bound + tick mechanics.
# ---------------------------------------------------------------------------


def test_iterations_bound_returns_normally():
    """iterations=N runs exactly N ticks and returns EXIT_OK."""
    client = FakeClient([_good_tick() for _ in range(5)])
    frames: list = []
    rc = fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=5,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=_silent_console(),
        on_frame=lambda i, s, m: frames.append((i, s, m)),
        screen=False,
    )
    assert rc == fq.EXIT_OK
    assert client.tick_calls == 5
    assert len(frames) == 5
    assert [f[0] for f in frames] == [1, 2, 3, 4, 5]


def test_sleep_called_between_ticks_not_after_last():
    """We sleep AFTER rendering a frame, but the iterations cap
    short-circuits before the trailing sleep so the loop ends
    promptly.
    """
    sleep_calls: list[float] = []
    client = FakeClient([_good_tick() for _ in range(3)])
    fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=2.5,
        iterations=3,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=sleep_calls.append,
        console=_silent_console(),
        screen=False,
    )
    # Three ticks, sleep happens between ticks 1->2 and 2->3 only.
    # After tick 3, the iterations check returns before sleeping.
    assert sleep_calls == [2.5, 2.5]


def test_owned_client_is_closed_on_normal_exit():
    """If the caller passes a Client, run_watch must NOT close it.
    If it builds its own from Config, it MUST close it. We can only
    fully test the latter end-to-end with a fake httpx transport;
    here we lock the "passed Client not closed" half by giving a
    FakeClient and asserting close_calls stayed zero.
    """
    client = FakeClient([_good_tick()])
    fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=1,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=_silent_console(),
        screen=False,
    )
    assert client.close_calls == 0


# ---------------------------------------------------------------------------
# format_mode: plain watch is unusual but allowed (PRD §Solution).
# ---------------------------------------------------------------------------


def test_format_mode_plain_renders_plain_output_in_watch():
    """watch + plain is allowed. The frame uses render_plain's ASCII
    layout (no Rich tables / box drawing).
    """
    client = FakeClient([_good_tick()])
    console, buf = _capturing_console()
    fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=1,
        format_mode="plain",
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=console,
        screen=False,
    )
    out = buf.getvalue()
    # Plain section anchors present.
    assert "TOTALS:" in out
    assert "RUNNERS (" in out
    # No Rich box-drawing in the plain watch frame.
    for ch in ("┏", "┃", "╭", "╮"):
        assert ch not in out


def test_format_mode_plain_stale_marker_prefixes_frame():
    """In plain mode the stale marker is a leading line, not a Panel."""
    client = FakeClient([_good_tick(), fq.ConnectionError("timeout")])
    console, buf = _capturing_console()
    fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=2.0,
        iterations=2,
        format_mode="plain",
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=console,
        screen=False,
    )
    out = buf.getvalue()
    assert "STALE (last good 14:03:11 UTC, retrying every 2s)" in out


# ---------------------------------------------------------------------------
# TTY fallback: screen=True on a non-terminal console downgrades.
# ---------------------------------------------------------------------------


def test_screen_true_on_non_tty_console_downgrades_with_warning(capsys):
    """screen=True but the console isn't a real terminal (StringIO,
    or piped stdout): Live's alternate-screen mode would corrupt
    output, so run_watch downgrades to screen=False and warns on
    stderr. The loop still completes normally.
    """
    client = FakeClient([_good_tick()])
    console = _silent_console()  # force_terminal=False -> is_terminal False
    rc = fq.run_watch(
        config=fq.Config(host=HOST, token="t", timeout=5.0, metrics_enabled=False),
        interval=0.0,
        iterations=1,
        client=client,
        clock=_stepping_clock(T0),
        sleep_fn=lambda _s: None,
        console=console,
        screen=True,  # request screen mode on a non-TTY
    )
    assert rc == fq.EXIT_OK
    err = capsys.readouterr().err
    assert "not a TTY" in err
    assert "downgrading watch" in err


# ---------------------------------------------------------------------------
# Helper unit tests.
# ---------------------------------------------------------------------------


def test_format_stale_marker_uses_utc_hms_and_interval():
    last_good = datetime(2026, 5, 27, 9, 5, 42, tzinfo=UTC)
    assert (
        fq._format_stale_marker(last_good, 2.0)
        == "STALE (last good 09:05:42 UTC, retrying every 2s)"
    )


def test_format_stale_marker_converts_non_utc_to_utc_first():
    """An accidental non-UTC last_good_at gets normalized to UTC
    before the HH:MM:SS slice, matching the rest of the wire which
    always uses UTC.
    """
    from datetime import timedelta
    plus_two = timezone(timedelta(hours=2))
    last_good = datetime(2026, 5, 27, 11, 5, 42, tzinfo=plus_two)
    # 11:05:42 +02:00 == 09:05:42 UTC.
    assert (
        fq._format_stale_marker(last_good, 5)
        == "STALE (last good 09:05:42 UTC, retrying every 5s)"
    )


def test_resolve_repos_for_jobs_uses_client_cache_once_per_id():
    """The helper that M6 will reuse for --once: each unique repo_id
    is looked up exactly once via Client.resolve_repo (which has its
    own per-process cache; we depend on that cache, not duplicate it).
    """

    class _CountingResolver:
        def __init__(self):
            self.calls: dict[int, int] = {}

        def resolve_repo(self, rid: int) -> str:
            self.calls[rid] = self.calls.get(rid, 0) + 1
            return f"resolved/{rid}"

    resolver = _CountingResolver()
    jobs = [
        _job(1, repo_id=85),
        _job(2, repo_id=85),
        _job(3, repo_id=86),
        _job(4, repo_id=85),
    ]
    out = fq._resolve_repos_for_jobs(resolver, jobs)
    assert out == {85: "resolved/85", 86: "resolved/86"}
    assert resolver.calls == {85: 1, 86: 1}


# ---------------------------------------------------------------------------
# Renderable composition: stale banner is present iff marker is set.
# ---------------------------------------------------------------------------


def test_watch_renderable_no_banner_when_no_marker():
    """Without a stale marker, _watch_renderable returns the M4 Rich
    Group with no STALE banner. We assert by rendered-text content
    rather than object identity (render_rich returns a fresh Group
    each call; there's no shared instance).
    """
    snap = fq.aggregate(
        runners=(),
        jobs=(),
        repo_names={},
        now=T0,
        host=HOST,
    )
    wrapped = fq._watch_renderable(snap, None)
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(wrapped)
    out = buf.getvalue()
    assert "STALE" not in out
    # And the regular snapshot panel header is still present.
    assert "snapshot" in out


def test_watch_renderable_with_marker_wraps_in_group_with_banner():
    from rich.console import Group
    snap = fq.aggregate(
        runners=(),
        jobs=(),
        repo_names={},
        now=T0,
        host=HOST,
    )
    wrapped = fq._watch_renderable(snap, "STALE (last good 14:03:11)")
    assert isinstance(wrapped, Group)
    # Rendered text contains the stale marker.
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(wrapped)
    out = buf.getvalue()
    assert "STALE (last good 14:03:11)" in out


# ---------------------------------------------------------------------------
# M6 carry-over: scrub-plumbing integration test (PRD #61 line 163).
#
# The unit test (test_build_error_renderable_scrubs_token in test_cli.py)
# exercises `_build_error_renderable` directly with an explicit
# `scrub_token=` argument. It does NOT prove that `run_watch` actually
# forwards `config.token` to that helper at the mid-loop call site. A
# future refactor that silently drops the kwarg would still pass the
# helper-level test while leaking the token into the Rich Live frame.
#
# This integration test pins the call-site wiring end-to-end: drive
# `run_watch` to a mid-loop AuthError whose `str(exc)` carries the
# token verbatim, capture the Rich frame written before re-raise, and
# assert the literal token does NOT appear anywhere in the output.
# ---------------------------------------------------------------------------


def test_run_watch_forwards_config_token_to_error_renderable_scrub():
    """Mid-loop AuthError whose message contains the token must render
    a one-frame error panel with the token masked. Proves run_watch
    passes `scrub_token=config.token` to `_build_error_renderable`.
    """
    secret = "ghp_LIVEsecret_should_NOT_leak_via_watch_panel_AAAAA"
    leaky_exc = fq.AuthError(
        f"401 unauthorized; raw header was 'token {secret}'"
    )
    client = FakeClient([_good_tick(), leaky_exc])
    console, buf = _capturing_console()
    with pytest.raises(fq.AuthError):
        fq.run_watch(
            config=fq.Config(host=HOST, token=secret, timeout=5.0, metrics_enabled=False),
            interval=0.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=lambda _s: None,
            console=console,
            screen=False,
            error_pause=0.0,
        )
    rendered = buf.getvalue()
    # The token must not appear in the rendered Live frame; the scrub
    # helper replaces it with "***" before composing the Rich Text body.
    assert secret not in rendered, (
        "config.token leaked into watch error frame -- "
        "run_watch dropped scrub_token=config.token at the "
        "_build_error_renderable call site"
    )
    assert "***" in rendered
    # And the panel rendered the expected human-readable headline,
    # confirming we are inspecting the actual error frame and not some
    # earlier good frame.
    assert "auth error" in rendered.lower() or "Authentication failed" in rendered


def test_run_watch_forwards_config_token_on_schema_drift_too():
    """Symmetric check: SchemaDrift mid-loop also routes through
    `_build_error_renderable(scrub_token=config.token)`. The token
    must not leak via the schema-drift panel either.
    """
    secret = "ghp_LIVEsecret_should_NOT_leak_via_schema_drift_panel"
    leaky_exc = fq.SchemaDrift(
        f"runners payload was None; auth header used token {secret}"
    )
    client = FakeClient([_good_tick(), leaky_exc])
    console, buf = _capturing_console()
    with pytest.raises(fq.SchemaDrift):
        fq.run_watch(
            config=fq.Config(host=HOST, token=secret, timeout=5.0, metrics_enabled=False),
            interval=0.0,
            iterations=10,
            client=client,
            clock=_stepping_clock(T0),
            sleep_fn=lambda _s: None,
            console=console,
            screen=False,
            error_pause=0.0,
        )
    rendered = buf.getvalue()
    assert secret not in rendered
    assert "***" in rendered
    assert "schema drift" in rendered.lower()
