#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx==0.27.2",
#     "rich==13.9.4",
# ]
# ///

"""fj-queue: Forgejo Actions runner & CI queue dashboard.

Read-only TUI/JSON view of the Forgejo admin Actions API. PRD #61.

This file is the single importable module. Layer boundaries:

  Config -> token resolution -> httpx Client wrapper
         -> fetch_runners() (paged via Link header)        | M1 (client)
         -> fetch_jobs()    (single unbounded call)        |
         -> resolve_repo()  (per-process cache)            |
                                                           |
         -> aggregate(runners, jobs, repo_names, now,      | M2 (aggregation)
                      *, host) -> Snapshot                 |   - pure, I/O-free
                                                           |   - clock-injected
                                                           |   - per-runner SUPERSET
                                                           |     label matching
                                                           |   - online = active|idle
                                                           |   - needs-gated jobs
                                                           |     excluded from
                                                           |     `unschedulable`

JSON serialization (M3), Plain + Rich rendering (M4), watch loop (M5) and
the argparse CLI (M6) build on top of the Snapshot returned by aggregate().

All Forgejo field-name knowledge lives in the client layer and is mapped
to stable internal names on the frozen dataclasses below.

PRD path: https://github.com/vtmocanu/fj-queue/issues/2
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Iterable, Literal, Mapping
from urllib.parse import urlsplit

import httpx


# ---------------------------------------------------------------------------
# Exit codes (PRD JSON contract -> Error contract)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_CONNECTION = 4
EXIT_SCHEMA_DRIFT = 5
# Ctrl-C in watch mode. POSIX convention: 128 + SIGINT(2) = 130. This is
# also CPython's natural exit code for an uncaught KeyboardInterrupt, so
# returning it keeps the watch loop consistent with htop/top/less/watch
# and lets wrapping shell scripts distinguish "operator aborted" from
# "loop completed" (EXIT_OK). Locked in PRD §Error contract.
EXIT_INTERRUPTED = 130

# Tool version (distinct from the JSON schema_version). Surfaced by
# `--version`. v1.0.0 = the first complete implementation of the PRD #61
# v1 contract.
__version__ = "1.0.0"


# ---------------------------------------------------------------------------
# Typed exception hierarchy. Each carries the exit code the CLI should use.
# Note: ConnectionError intentionally shadows the builtin inside this module;
# external callers reference it as `fj_queue.ConnectionError`. We never `raise`
# or `except` the builtin within this file.
# ---------------------------------------------------------------------------


class FjQueueError(Exception):
    """Base error. Subclasses carry an explicit exit_code + error_code.

    `exit_code` (int) is what the CLI process returns; `error_code`
    (str) is what M3's JSON error envelope emits in the wire `code`
    field. The pair is stable wire contract; bump schema_version on
    breaking changes.
    """

    exit_code: int = 1
    error_code: str = "internal"


class ConfigError(FjQueueError):
    """Bad/missing CLI config (e.g. no token)."""

    exit_code = EXIT_USAGE
    error_code = "usage"


class AuthError(FjQueueError):
    """401 unauthorized or 403 forbidden (token missing/non-admin)."""

    exit_code = EXIT_AUTH
    error_code = "auth"


class ConnectionError(FjQueueError):  # noqa: A001 (intentional shadow within module)
    """Network/HTTP failure: timeout, 5xx, 429, refused, etc."""

    exit_code = EXIT_CONNECTION
    error_code = "connection"


class SchemaDrift(FjQueueError):
    """Forgejo response shape does not match what M1 knows how to parse."""

    exit_code = EXIT_SCHEMA_DRIFT
    error_code = "schema_drift"


# ---------------------------------------------------------------------------
# Config (built once from argparse in M6; usable standalone in M1).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    host: str = "git.example.com"
    token: str = ""
    timeout: float = 10.0
    # Per-pod runner CPU/memory metrics (Prometheus). Always-on with
    # graceful degradation; --no-metrics flips `metrics_enabled` off.
    # The metrics source is Prometheus, a SEPARATE endpoint from the
    # Forgejo host with NO auth (see fetch_runner_pods); these fields
    # never carry the Forgejo token.
    metrics_enabled: bool = True
    metrics_url: str = "https://prometheus.example.com"
    metrics_namespace: str = "forgejo-runner"
    metrics_timeout: float = 3.0
    metrics_cluster: str = "auto"

    @property
    def base_url(self) -> str:
        h = self.host.strip()
        if not h.startswith(("http://", "https://")):
            h = "https://" + h
        return h.rstrip("/")

    # Token-masking repr (and str) so the secret never leaks into log
    # lines, JSON error envelopes (M3), Rich panels (M4), or watch-mode
    # error displays (M5). M1 today does not log a Config anywhere, but
    # downstream layers will -- fix at the source while it is cheap.
    def __repr__(self) -> str:
        masked = "***" if self.token else ""
        return (
            f"Config(host={self.host!r}, "
            f"token={masked!r}, "
            f"timeout={self.timeout})"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# Token resolution.
#
# Precedence (PRD Authentication, with Open Question #2 default applied):
#   1. --token arg
#   2. $FORGEJO_TOKEN env
#   (tea-config fallback intentionally dropped: would pull in pyyaml for one
#   optional path. Re-add behind --token-from-tea if asked.)
# ---------------------------------------------------------------------------


def resolve_token(
    arg_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the token to use, or raise ConfigError if neither source has one."""
    if env is None:
        env = os.environ
    if arg_token:
        return arg_token
    from_env = env.get("FORGEJO_TOKEN", "")
    if from_env:
        return from_env
    raise ConfigError(
        "no token: pass --token or set FORGEJO_TOKEN in the environment"
    )


# Default Prometheus base URL for the runner CPU/memory metrics. NO auth;
# reachable from the laptop. Overridable via --metrics-url or the
# FJ_QUEUE_METRICS_URL env var (mirrors the FORGEJO_TOKEN env pattern).
_DEFAULT_METRICS_URL = "https://prometheus.example.com"


def resolve_metrics_url(
    arg_url: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the Prometheus base URL to use.

    Precedence: --metrics-url arg > $FJ_QUEUE_METRICS_URL env > default.
    Unlike resolve_token there is no error case: a default always exists.
    """
    if env is None:
        env = os.environ
    if arg_url:
        return arg_url
    from_env = env.get("FJ_QUEUE_METRICS_URL", "")
    if from_env:
        return from_env
    return _DEFAULT_METRICS_URL


# ---------------------------------------------------------------------------
# Frozen models. M1 owns Runner + RawJob; M2 will add the aggregated Job +
# RepoBreakdown + Warning + Snapshot. RawJob is distinct from M2's enriched
# Job: it carries only what the API returned, with type normalization.
#
# Field-name mapping (Forgejo internal -> stable internal name kept here):
#
#   GET /api/v1/admin/actions/runners       (paginated; bare JSON array)
#     id        -> Runner.id        (int)
#     name      -> Runner.name      (str)
#     status    -> Runner.status    (str: offline | idle | active; not enum-constrained,
#                                    parse defensively, unknown values treated as offline
#                                    downstream in M2)
#     version   -> Runner.version   (str)
#     labels    -> Runner.labels    (tuple[str, ...])
#     ephemeral -> Runner.ephemeral (bool)
#     (uuid, owner_id, repo_id, description on the wire are intentionally ignored;
#     fj-queue presents instance-wide visibility only.)
#
#   GET /api/v1/admin/actions/runners/jobs  (NOT paginated; bare JSON array)
#     id       -> RawJob.id        (int)
#     name     -> RawJob.name      (str)
#     status   -> RawJob.status    (str: waiting | running; parse defensively)
#     repo_id  -> RawJob.repo_id   (int)
#     owner_id -> RawJob.owner_id  (int)
#     runs_on  -> RawJob.runs_on   (tuple[str, ...])
#     needs    -> RawJob.needs     (tuple[str, ...])
#     task_id  -> RawJob.task_id   (int; 0 while waiting, non-zero once running)
#     attempt  -> RawJob.attempt   (int; >1 indicates a rerun)
#     handle   -> RawJob.handle    (str; opaque dedupe key)
#
#   GET /api/v1/repositories/{id}
#     full_name -> str returned by Client.resolve_repo (cached)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Runner:
    id: int
    name: str
    status: str
    version: str
    labels: tuple[str, ...]
    ephemeral: bool


@dataclass(frozen=True)
class RawJob:
    """A job as it arrives from the queue endpoint, pre-aggregation.

    Kept deliberately distinct from M2's enriched Job (which will add a
    `repo` slug, queue `position`, and `blocked_reason`).
    """

    id: int
    name: str
    status: str
    repo_id: int
    owner_id: int
    runs_on: tuple[str, ...]
    needs: tuple[str, ...]
    task_id: int
    attempt: int
    handle: str


def _coerce_str_list(raw: object) -> tuple[str, ...]:
    """Defensive: turn None / missing / list[Any] into tuple[str, ...]."""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise SchemaDrift(f"expected list, got {type(raw).__name__}: {raw!r}")
    return tuple(str(x) for x in raw)


def _normalize_runner(raw: dict) -> Runner:
    try:
        return Runner(
            id=int(raw["id"]),
            name=str(raw.get("name", "")),
            status=str(raw.get("status", "offline")),
            version=str(raw.get("version", "")),
            labels=_coerce_str_list(raw.get("labels")),
            ephemeral=bool(raw.get("ephemeral", False)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise SchemaDrift(f"runner parse failed: {e!r}; raw={raw!r}") from e


def _normalize_job(raw: dict) -> RawJob:
    try:
        return RawJob(
            id=int(raw["id"]),
            name=str(raw.get("name", "")),
            status=str(raw.get("status", "")),
            repo_id=int(raw.get("repo_id", 0)),
            owner_id=int(raw.get("owner_id", 0)),
            runs_on=_coerce_str_list(raw.get("runs_on")),
            needs=_coerce_str_list(raw.get("needs")),
            task_id=int(raw.get("task_id", 0)),
            attempt=int(raw.get("attempt", 1)),
            handle=str(raw.get("handle", "")),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise SchemaDrift(f"job parse failed: {e!r}; raw={raw!r}") from e


# ---------------------------------------------------------------------------
# Link-header parsing (RFC 5988). Forgejo emits absolute URLs like
#   Link: <https://git.example.com/api/v1/admin/actions/runners?limit=50&page=2>; rel="next",
#         <https://...?page=2>; rel="last"
# We just need the `rel="next"` URL.
# ---------------------------------------------------------------------------


def _parse_next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        seg = part.strip()
        if not seg.startswith("<"):
            continue
        end = seg.find(">")
        if end < 0:
            continue
        url = seg[1:end]
        params = seg[end + 1 :]
        # Tolerate both rel="next" and rel=next forms.
        if 'rel="next"' in params or "rel=next" in params:
            return url
    return None


def _enforce_same_host(next_url: str, expected_host: str) -> str:
    """Trust-boundary check on a Link: rel=\"next\" URL.

    The admin token sits in the Authorization header on the long-lived
    httpx Client. httpx does NOT auto-redact Authorization on a manually
    issued cross-origin request, so a misconfigured proxy or a future
    Forgejo bug emitting an absolute next-link pointing at another host
    would silently leak our admin token.

    Policy: if the next URL is absolute, its host (and scheme, to block
    http: downgrades) MUST match the configured host. Mismatch -> raise
    SchemaDrift (the server emitted something outside the contract we
    are willing to trust). Relative / path-only URLs pass through.

    expected_host is taken from Config.host; Config normalizes to https
    in base_url, so we compare against https + host.
    """
    parts = urlsplit(next_url)
    # Relative URL or path-only: no host to police, trust httpx + base_url.
    if not parts.netloc:
        return next_url
    # Strip a possible :port from netloc for the comparison.
    next_host = parts.hostname or ""
    cfg_host = expected_host.strip().lower()
    # Tolerate the user passing host with a scheme (Config.base_url does).
    if cfg_host.startswith("https://"):
        cfg_host = cfg_host[len("https://") :]
    elif cfg_host.startswith("http://"):
        cfg_host = cfg_host[len("http://") :]
    cfg_host = cfg_host.rstrip("/").split("/", 1)[0]
    if next_host.lower() != cfg_host:
        raise SchemaDrift(
            f"refusing to follow cross-host Link: rel=\"next\" "
            f"(host={next_host!r}, expected={cfg_host!r}); "
            f"would leak Authorization header"
        )
    # We accept https only. If the server downgrades to http on the next
    # link, treat that as a leak risk too.
    if parts.scheme and parts.scheme != "https":
        raise SchemaDrift(
            f"refusing to follow non-https Link: rel=\"next\" "
            f"(scheme={parts.scheme!r}); would leak Authorization header"
        )
    return next_url


# ---------------------------------------------------------------------------
# Client. The ONLY place that knows Forgejo URL paths and field names.
# Higher layers consume typed models.
# ---------------------------------------------------------------------------


# Hard cap on pagination follow-through. Real instances should have under a
# dozen pages for runners; 200 is a refusal-to-spin guard against a runaway
# Link header loop.
_RUNNER_PAGE_CAP = 200

# A larger page size than Forgejo's ~30 default reduces round trips on the
# runner endpoint while still letting Link-header pagination kick in.
_RUNNER_PAGE_SIZE = 50


class Client:
    """Thin httpx wrapper around the Forgejo admin Actions API.

    Constructed with a Config. Holds a per-process repo-id -> slug cache so
    repeated `resolve_repo` calls in a single run do not re-hit the API.
    """

    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.config = config
        self._owns_http = http is None
        if http is None:
            try:
                http = httpx.Client(
                    base_url=config.base_url,
                    headers={"Authorization": f"token {config.token}"},
                    timeout=config.timeout,
                )
            except httpx.InvalidURL as e:
                # A malformed --host (CRLF injection, scheme garbage,
                # unsplittable netloc, etc.) reaches httpx as an
                # InvalidURL on base_url. Without this catch the error
                # escapes the typed FjQueueError taxonomy, stdout
                # ends up empty (violates the PRD JSON-contract
                # guarantee that even failures emit a JSON envelope),
                # and the exit code is 1 instead of the typed 2.
                raise ConfigError(
                    f"invalid --host {config.host!r}: {e}"
                ) from e
        self._http = http
        self._repo_cache: dict[int, str] = {}

    # context manager support so `with Client(cfg) as c:` closes the transport
    def __enter__(self) -> "Client":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # -- internal: GET with typed error mapping --------------------------

    def _get(self, url_or_path: str) -> httpx.Response:
        try:
            r = self._http.get(url_or_path)
        except httpx.TimeoutException as e:
            raise ConnectionError(f"timeout calling {url_or_path}") from e
        except httpx.HTTPError as e:
            raise ConnectionError(
                f"connection error calling {url_or_path}: {e}"
            ) from e

        if r.status_code == 401:
            raise AuthError("401 unauthorized: check --token / FORGEJO_TOKEN")
        if r.status_code == 403:
            raise AuthError(
                "403 forbidden: admin scope required for "
                "/api/v1/admin/actions/* endpoints"
            )
        if r.status_code == 429:
            raise ConnectionError(f"429 rate-limited by {self.config.host}")
        if 500 <= r.status_code < 600:
            raise ConnectionError(
                f"{r.status_code} server error on {url_or_path}"
            )
        if r.status_code >= 400:
            # Intentionally NO body fragment here: the server-controlled
            # response body is an unbounded surface that flows into
            # error.message on stdout under --format json. Status code
            # plus URL path is enough for ops triage; the full body
            # can be logged to stderr by M5/M6 if a CLI flag wants it.
            raise ConnectionError(f"{r.status_code} on {url_or_path}")
        return r

    # -- public --------------------------------------------------------

    def fetch_runners(self) -> list[Runner]:
        """Fetch all runners, following Link: rel=\"next\" pagination.

        Forgejo's default page size is ~30 and the response carries a
        proper RFC 5988 Link header (verified live against v15.0.2). We
        pass a larger limit to cut round trips but still walk every
        next-link until exhausted.

        Forgejo quirk (verified live 2026-05-27 on v15.0.2): if you call
        the endpoint with `?limit=N` but no `&page=...`, the server
        IGNORES the limit and returns the full set, yet still emits a
        `Link: rel="next"` pointing at `page=2`. Following that link
        then double-counts the tail runners. So:
          1. Always pass `page=1` explicitly on the first request, which
             makes the limit honored and the pagination predictable.
          2. Dedupe by runner id as a defensive belt-and-braces guard.
        """
        runners: list[Runner] = []
        seen_ids: set[int] = set()
        next_path: str | None = (
            f"/api/v1/admin/actions/runners"
            f"?limit={_RUNNER_PAGE_SIZE}&page=1"
        )
        pages = 0
        while next_path:
            r = self._get(next_path)
            try:
                body = r.json()
            except ValueError as e:
                raise SchemaDrift(
                    f"non-JSON runners response from {next_path}: {e}"
                ) from e
            # Forgejo returns a bare list (verified live). Tolerate an
            # enveloped {"runners": [...]} shape too in case a future
            # version adds one.
            items = body if isinstance(body, list) else (
                body.get("runners") if isinstance(body, dict) else None
            )
            if items is None:
                raise SchemaDrift(
                    f"unexpected runners payload shape: "
                    f"{type(body).__name__}"
                )
            for raw in items:
                if not isinstance(raw, dict):
                    raise SchemaDrift(
                        f"runner item not an object: {type(raw).__name__}"
                    )
                runner = _normalize_runner(raw)
                if runner.id in seen_ids:
                    continue
                seen_ids.add(runner.id)
                runners.append(runner)
            raw_next = _parse_next_link(r.headers.get("link"))
            # Trust-boundary check: refuse to send the admin token off-host.
            next_path = (
                _enforce_same_host(raw_next, self.config.host)
                if raw_next
                else None
            )
            pages += 1
            if pages > _RUNNER_PAGE_CAP:
                raise ConnectionError(
                    f"runner pagination exceeded {_RUNNER_PAGE_CAP} pages"
                )
        return runners

    def fetch_jobs(self) -> list[RawJob]:
        """Fetch the waiting+running jobs list. Endpoint does NOT paginate."""
        r = self._get("/api/v1/admin/actions/runners/jobs")
        try:
            body = r.json()
        except ValueError as e:
            raise SchemaDrift(f"non-JSON jobs response: {e}") from e
        # Forgejo v15.0.2 quirk (verified live against git.example.com on
        # 2026-05-28): when the live queue is empty, the endpoint returns
        # the bare JSON literal `null` rather than `[]`. Treat it as the
        # empty list per the wire contract, NOT as a schema break.
        if body is None:
            return []
        items = body if isinstance(body, list) else (
            body.get("jobs") if isinstance(body, dict) else None
        )
        if items is None:
            raise SchemaDrift(
                f"unexpected jobs payload shape: {type(body).__name__}"
            )
        out: list[RawJob] = []
        for raw in items:
            if not isinstance(raw, dict):
                raise SchemaDrift(
                    f"job item not an object: {type(raw).__name__}"
                )
            out.append(_normalize_job(raw))
        return out

    def resolve_repo(self, repo_id: int) -> str:
        """Return `owner/repo` for repo_id, with a per-process cache.

        Never raises for "looked up and missed": 404/403/timeout/network
        errors all fall back to the printable label `repo#<id>` so that a
        single bad repo lookup can never abort an aggregation. Caller
        sees a non-empty string, always.
        """
        cached = self._repo_cache.get(repo_id)
        if cached is not None:
            return cached
        fallback = f"repo#{repo_id}"
        try:
            r = self._http.get(f"/api/v1/repositories/{repo_id}")
        except httpx.HTTPError:
            self._repo_cache[repo_id] = fallback
            return fallback
        if r.status_code != 200:
            self._repo_cache[repo_id] = fallback
            return fallback
        try:
            body = r.json()
        except ValueError:
            self._repo_cache[repo_id] = fallback
            return fallback
        slug = body.get("full_name") if isinstance(body, dict) else None
        result = str(slug) if slug else fallback
        self._repo_cache[repo_id] = result
        return result


# ---------------------------------------------------------------------------
# Module-level convenience wrappers. Useful for the M1 live-validation
# smoke (the snippet in the task brief) and for downstream milestones that
# do not need to pass a pre-built Client around.
# ---------------------------------------------------------------------------


def _default_config() -> Config:
    return Config(token=resolve_token())


def fetch_runners(config: Config | None = None) -> list[Runner]:
    cfg = config or _default_config()
    with Client(cfg) as c:
        return c.fetch_runners()


def fetch_jobs(config: Config | None = None) -> list[RawJob]:
    cfg = config or _default_config()
    with Client(cfg) as c:
        return c.fetch_jobs()


def resolve_repo(repo_id: int, config: Config | None = None) -> str:
    cfg = config or _default_config()
    with Client(cfg) as c:
        return c.resolve_repo(repo_id)


# M2..M6 will extend this module. M1 has no CLI entry point yet (M6 lands
# argparse + --format dispatch). Running this file directly is a no-op
# from M1's perspective; tests import the symbols above.


# ===========================================================================
# Prometheus metrics client (per-pod runner CPU/memory).
#
# DELIBERATELY ISOLATED from the Forgejo `Client` above: a separate httpx
# client, a different base URL (prometheus.example.com), and NO Authorization
# header. The Forgejo admin token must NEVER reach Prometheus. This layer
# owns all Prometheus URL-path + PromQL knowledge and maps the HTTP API's
# instant-vector response into typed `PodResource` rows.
#
# The Forgejo admin API exposes id/name/status/version/labels/ephemeral
# for runners but NO CPU/memory, and there is no per-runner-row join key
# (every live runner shares the API name `k8s-runner`; the API never
# exposes the pod name; Prometheus never sees the runner UUID). So this is
# a SEPARATE per-pod section, not fields on the Runner inventory rows.
#
# Always-on with graceful degradation: any failure (timeout, HTTP error,
# malformed body, bad URL) returns `((), error_string)` and NEVER raises,
# so the dashboard still renders with an "unavailable" line.
#
# Verified PromQL (live against prometheus.example.com, 2026-05-29):
#   1. CPU cores per pod (combines the `runner` + `dind` containers):
#      sum by (pod) (rate(container_cpu_usage_seconds_total
#        {namespace="<ns>", container!=""}[5m]))
#   2. Memory working-set bytes per pod (combined containers):
#      sum by (pod) (container_memory_working_set_bytes
#        {namespace="<ns>", container!=""})
#   3. pod -> node map (read the `node` label off each series):
#      kube_pod_info{namespace="<ns>"}
# Join the three by `pod`. cpu_cores = float(value); memory_bytes =
# int(float(value)). Blue/green: both clusters report cluster="k8s-cluster";
# the live one is told apart by the node prefix (k8s-node-* vs
# k8s-node-*). --metrics-cluster green|blue keeps only pods whose node
# starts with `k8s-<color>-`; `auto` applies no filter.
# ===========================================================================


@dataclass(frozen=True)
class PodResource:
    """One runner pod's combined-container resource usage.

    `cpu_cores` is the 5m-rate CPU usage in cores; `memory_bytes` is the
    working-set memory in raw bytes (renderers format to MiB; JSON keeps
    the raw integer). `node` is the Kubernetes node the pod runs on,
    used both for display and for the blue/green cluster filter.

    `cpu_limit_cores` / `memory_limit_bytes` are the configured pod
    resource limits (summed across containers), or None when no limit is
    set. CURRENT reality (verified 2026-05-29): memory limits ARE set
    (~17.4 GiB/pod) but CPU limits are NOT, so cpu_limit_cores is
    typically None. Renderers show `usage / limit` and fall back to a
    dash for a None limit; JSON keeps raw numbers (null when absent).
    """

    pod: str
    node: str
    cpu_cores: float
    memory_bytes: int
    cpu_limit_cores: float | None = None
    memory_limit_bytes: int | None = None


# Rate window for the CPU query, surfaced in the JSON `metrics.rate_window`.
_PROM_RATE_WINDOW = "5m"

# Sentinel `metrics.error` value when metrics are turned off via
# --no-metrics. Distinct from None (a successful fetch with zero pods)
# so JSON consumers can tell the two apart.
METRICS_DISABLED = "disabled"

# PromQL templates. `{ns}` is filled with the metrics namespace; literal
# PromQL braces are doubled for str.format().
# The rate window is interpolated from _PROM_RATE_WINDOW so the literal in
# the query and the JSON `metrics.rate_window` can never drift apart.
# `{ns}` stays a str.format placeholder (filled per call).
_PROM_CPU_QUERY = (
    'sum by (pod) (rate(container_cpu_usage_seconds_total'
    '{{namespace="{ns}", container!=""}}[' + _PROM_RATE_WINDOW + ']))'
)
_PROM_MEM_QUERY = (
    'sum by (pod) (container_memory_working_set_bytes'
    '{{namespace="{ns}", container!=""}})'
)
_PROM_INFO_QUERY = 'kube_pod_info{{namespace="{ns}"}}'
# Configured pod resource limits (summed across containers). Memory limits
# ARE set (~17.4 GiB/pod); CPU limits are NOT (the cpu query returns no
# series), so the cpu-limit map is typically empty -> cpu_limit_cores=None.
_PROM_MEM_LIMIT_QUERY = (
    'sum by (pod) (kube_pod_container_resource_limits'
    '{{namespace="{ns}", resource="memory"}})'
)
_PROM_CPU_LIMIT_QUERY = (
    'sum by (pod) (kube_pod_container_resource_limits'
    '{{namespace="{ns}", resource="cpu"}})'
)


class _MetricsError(Exception):
    """Internal: a structural problem in a Prometheus response. Caught
    inside fetch_runner_pods and converted to the graceful error string;
    never escapes the module.
    """


def _prom_query_vector(http: httpx.Client, query: str) -> list:
    """Issue one Prometheus instant query and return its `result` vector.

    Raises _MetricsError on any non-success envelope; lets httpx
    transport errors (timeout / connection) propagate to the caller.
    """
    r = http.get("/api/v1/query", params={"query": query})
    if r.status_code != 200:
        raise _MetricsError(f"prometheus HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as e:
        raise _MetricsError(f"non-JSON prometheus response: {e}") from e
    if not isinstance(body, dict) or body.get("status") != "success":
        status = body.get("status") if isinstance(body, dict) else type(body).__name__
        raise _MetricsError(f"prometheus status != success: {status!r}")
    data = body.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        raise _MetricsError("prometheus response missing result vector")
    return result


def _prom_scalar_by_pod(result: list, parse) -> dict[str, object]:
    """Build a {pod: parse(value)} map from an instant-vector result.

    Series missing a `pod` label or a usable value are skipped (defensive);
    a value that fails `parse` skips that one series, not the whole query.
    """
    out: dict[str, object] = {}
    for series in result:
        if not isinstance(series, dict):
            continue
        metric = series.get("metric") or {}
        pod = metric.get("pod")
        value = series.get("value")
        if not pod or not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        try:
            out[str(pod)] = parse(value[1])
        except (TypeError, ValueError):
            continue
    return out


def _prom_node_by_pod(result: list) -> dict[str, str]:
    """Build a {pod: node} map from a kube_pod_info result vector."""
    out: dict[str, str] = {}
    for series in result:
        if not isinstance(series, dict):
            continue
        metric = series.get("metric") or {}
        pod = metric.get("pod")
        if pod:
            out[str(pod)] = str(metric.get("node", ""))
    return out


def _prom_scalar(result: list, default: float = 0.0) -> float:
    """First series' value as a float from a scalar (no-`by`) query.

    Returns `default` (0.0) when the vector is empty (metric not scraped
    yet), the value is unparseable, or it is NaN. So a freshly-started
    NCPS with no samples reads as 0.0, not an error.
    """
    for series in result:
        if not isinstance(series, dict):
            continue
        value = series.get("value")
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        try:
            f = float(value[1])
        except (TypeError, ValueError):
            return default
        # NaN (Prometheus emits "NaN" for an empty rate window).
        if f != f:
            return default
        return f
    return default


def fetch_runner_pods(
    metrics_url: str,
    *,
    namespace: str = "forgejo-runner",
    cluster: str = "auto",
    timeout: float = 3.0,
    http: httpx.Client | None = None,
) -> tuple[tuple[PodResource, ...], str | None]:
    """Fetch combined-container CPU/memory per runner pod from Prometheus.

    Returns `(pods, None)` on success or `((), error_string)` on ANY
    failure. NEVER raises: callers (the watch loop, --once) rely on this
    so a Prometheus outage degrades gracefully instead of taking down
    the whole dashboard.

    The three PromQL queries (CPU, memory, pod->node) are joined by pod.
    With `cluster` in {green, blue}, only pods whose node starts with
    `k8s-<cluster>-` survive (blue/green disambiguation); `auto` keeps
    all. Results are sorted by pod name for deterministic output.

    A dedicated httpx client (no auth header, Prometheus base URL) is
    used so the Forgejo admin token can never leak to Prometheus.
    """
    base = (metrics_url or "").strip().rstrip("/")
    owns = http is None
    client = http
    # The ENTIRE body (client construction, the five queries, and the
    # pod-building/join loop) sits under one guard so the "never raises"
    # contract actually holds: any failure -> ((), error_str). The finally
    # closes a client we own even on the success path.
    try:
        if client is None:
            client = httpx.Client(base_url=base, timeout=timeout)
        cpu_by_pod = _prom_scalar_by_pod(
            _prom_query_vector(client, _PROM_CPU_QUERY.format(ns=namespace)),
            float,
        )
        mem_by_pod = _prom_scalar_by_pod(
            _prom_query_vector(client, _PROM_MEM_QUERY.format(ns=namespace)),
            lambda v: int(float(v)),
        )
        node_by_pod = _prom_node_by_pod(
            _prom_query_vector(client, _PROM_INFO_QUERY.format(ns=namespace)),
        )
        mem_limit_by_pod = _prom_scalar_by_pod(
            _prom_query_vector(
                client, _PROM_MEM_LIMIT_QUERY.format(ns=namespace)
            ),
            lambda v: int(float(v)),
        )
        cpu_limit_by_pod = _prom_scalar_by_pod(
            _prom_query_vector(
                client, _PROM_CPU_LIMIT_QUERY.format(ns=namespace)
            ),
            float,
        )

        pods: list[PodResource] = []
        for pod in sorted(set(cpu_by_pod) | set(mem_by_pod)):
            node = node_by_pod.get(pod, "")
            if cluster in ("green", "blue") and not node.startswith(
                f"k8s-{cluster}-"
            ):
                continue
            cpu_limit = cpu_limit_by_pod.get(pod)
            mem_limit = mem_limit_by_pod.get(pod)
            pods.append(
                PodResource(
                    pod=pod,
                    node=node,
                    cpu_cores=float(cpu_by_pod.get(pod, 0.0)),
                    memory_bytes=int(mem_by_pod.get(pod, 0)),
                    cpu_limit_cores=(None if cpu_limit is None else float(cpu_limit)),
                    memory_limit_bytes=(None if mem_limit is None else int(mem_limit)),
                )
            )
        return tuple(pods), None
    except _MetricsError as e:
        return (), str(e)
    except httpx.InvalidURL as e:
        return (), f"invalid metrics url {metrics_url!r}: {e}"
    except httpx.TimeoutException:
        return (), f"timeout querying prometheus at {base or metrics_url!r}"
    except httpx.HTTPError as e:
        return (), f"connection error querying prometheus: {e}"
    except Exception as e:  # noqa: BLE001 (last-resort: never crash the dashboard)
        return (), f"prometheus query failed: {e}"
    finally:
        if owns and client is not None:
            client.close()


# ---------------------------------------------------------------------------
# NCPS (nix cache proxy, nix-cache.example.com) active/idle status.
#
# Same isolated, no-auth Prometheus client as fetch_runner_pods; same
# never-raises contract. NCPS exposes a single instance (job="ncps"), so
# all four queries return a single scalar (no `by` label). The rate window
# is its own constant (NCPS scrapes every 30s, so 2m is the safe window)
# and is interpolated into the query string so the literal can't drift.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NcpsStatus:
    """NCPS cache activity at snapshot time.

    `active` iff it is serving requests right now (requests_per_sec > 0)
    or has any in-flight requests; otherwise idle. The throughput /
    upstream-miss / bytes fields back the human one-liner. All are raw
    numbers on the JSON wire; the renderers format them.
    """

    active: bool
    requests_per_sec: float
    inflight: int
    upstream_per_sec: float  # cache misses/sec (NCPS -> cache.nixos.org)
    bytes_per_sec: float


_NCPS_RATE_WINDOW = "2m"

# job="ncps" is hardcoded (single instance); no namespace placeholder. The
# rate window is interpolated from _NCPS_RATE_WINDOW (single source of
# truth, same pattern as _PROM_CPU_QUERY).
_NCPS_REQUESTS_QUERY = (
    'sum(rate(request_duration_millis_milliseconds_count'
    '{job="ncps"}[' + _NCPS_RATE_WINDOW + ']))'
)
_NCPS_INFLIGHT_QUERY = 'sum(requests_inflight{job="ncps"})'
_NCPS_UPSTREAM_QUERY = (
    'sum(rate(http_client_request_duration_seconds_count'
    '{job="ncps"}[' + _NCPS_RATE_WINDOW + ']))'
)
# Throughput = the NCPS pod's network egress (cAdvisor), NOT
# response_size_bytes_sum: NCPS doesn't record sizes for streamed nar
# bodies, so that sum is structurally 0. Egress is dominated by served
# nars (a cache miss's upstream download lands on RECEIVE, not transmit).
# This metric is keyed by namespace="ncps" (it has NO job="ncps" label).
_NCPS_BYTES_QUERY = (
    'sum(rate(container_network_transmit_bytes_total'
    '{namespace="ncps"}[' + _NCPS_RATE_WINDOW + ']))'
)


def fetch_ncps_status(
    metrics_url: str,
    *,
    timeout: float = 3.0,
    http: httpx.Client | None = None,
) -> NcpsStatus | None:
    """Fetch NCPS active/idle status from Prometheus.

    Returns an NcpsStatus on success or None on ANY failure (timeout,
    HTTP error, malformed body, bad URL). NEVER raises: callers treat
    None as "unavailable" and keep rendering the rest of the dashboard.
    Missing/NaN metrics degrade to 0.0 (a freshly-started NCPS).
    """
    base = (metrics_url or "").strip().rstrip("/")
    owns = http is None
    client = http
    try:
        if client is None:
            client = httpx.Client(base_url=base, timeout=timeout)
        req = _prom_scalar(_prom_query_vector(client, _NCPS_REQUESTS_QUERY))
        inflight = _prom_scalar(_prom_query_vector(client, _NCPS_INFLIGHT_QUERY))
        upstream = _prom_scalar(_prom_query_vector(client, _NCPS_UPSTREAM_QUERY))
        bytes_ps = _prom_scalar(_prom_query_vector(client, _NCPS_BYTES_QUERY))
        inflight_int = int(inflight)
        return NcpsStatus(
            active=(req > 0.0 or inflight_int > 0),
            requests_per_sec=req,
            inflight=inflight_int,
            upstream_per_sec=upstream,
            bytes_per_sec=bytes_ps,
        )
    except Exception:  # noqa: BLE001 (never crash the dashboard on metrics)
        return None
    finally:
        if owns and client is not None:
            client.close()


# ===========================================================================
# M2 -- Aggregation + scheduling logic.
#
# This block is pure: no I/O, no clock reads, no module-level state. The
# caller supplies a pre-fetched runners list, a pre-fetched jobs list, a
# pre-resolved repo_id -> slug map (built via Client.resolve_repo), and a
# `now` datetime. aggregate() returns the canonical Snapshot consumed by
# all three renderers (M3 JSON, M4 plain + Rich).
#
# Source-verified correctness anchors (PRD §Scheduling semantics):
#
#   1. Schedulability is per-runner SUPERSET label matching:
#        a job is schedulable iff at least one *online* runner exists whose
#        labels set is a superset of the job's runs_on
#        (equivalently: job.runs_on subset_of runner.labels).
#      The naive "union of all online runners' labels intersects runs_on"
#      rule is WRONG: a job needing [docker, gpu] with one docker-only
#      runner and one gpu-only runner has a non-empty union-intersection
#      yet no single runner can host it. Source: Forgejo
#      models/actions/run_job.go.
#
#   2. Online means status in {active, idle}. idle is NOT offline; an idle
#      runner is registered, polling, and fully available -- treating idle
#      as offline would mark every waiting job "stuck" during the normal
#      quiet state.
#
#   3. blocked_reason derivation, in priority order:
#        * status == "running"            -> None
#        * status == "waiting", needs != []
#                                         -> "blocked_on_needs"
#                                            (NOT a warning; excluded from
#                                             `unschedulable_labels` to
#                                             avoid false positives, per
#                                             PRD risk mitigation)
#        * status == "waiting", needs == [], some online runner satisfies
#                                            runs_on subset_of labels
#                                         -> "waiting_for_runner"
#        * status == "waiting", needs == [], no online runner can
#                                         -> "unschedulable" + warning
#
#   4. Queue order: jobs response arrives id DESC. M2 re-sorts to id ASC
#      as the documented FIFO approximation. `position` is 1-based over
#      all waiting jobs (including needs-gated ones).
#
#   5. Ordering contracts (used by M3 JSON serialization for byte-stable
#      output): runners by name, per_repo by repo slug, queue by job id
#      asc, warnings by job_id, schedulable_labels alphabetical.
#
# Empty-runs_on note (intentional design choice, flagged for review):
# under rule 1, an empty runs_on is trivially a subset of every label
# set, so any online runner satisfies it -> waiting_for_runner (not
# unschedulable). The PRD JSON-contract example shows job_id 79969 with
# runs_on=[] as an unschedulable_labels warning, BUT the [docker]+[gpu]
# example in the same PRD section confirms the formal rule (job ⊆
# runner) which makes empty-runs_on satisfiable. We follow the formal
# rule strictly; the all-offline path still flags empty-runs_on as
# unschedulable when there are zero online runners.
# ===========================================================================


# Runner status strings the API may emit. Anything outside this trio is
# treated as offline downstream (defensive: PRD says the swagger does
# not enum-constrain the field).
_ONLINE_STATUSES = frozenset({"active", "idle"})

# Job status strings of interest. Jobs response only emits waiting+running;
# fully blocked (deps unmet) jobs are omitted by the endpoint.
_STATUS_WAITING = "waiting"
_STATUS_RUNNING = "running"

# blocked_reason vocabulary (PRD JSON contract).
BLOCKED_UNSCHEDULABLE = "unschedulable"
BLOCKED_ON_NEEDS = "blocked_on_needs"
BLOCKED_WAITING_FOR_RUNNER = "waiting_for_runner"

# Warning vocabulary (PRD JSON contract).
WARN_UNSCHEDULABLE_LABELS = "unschedulable_labels"


def is_online(runner: Runner) -> bool:
    """status in {active, idle}. Unknown values fall through as offline."""
    return runner.status in _ONLINE_STATUSES


# ---------------------------------------------------------------------------
# Frozen aggregation models.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """A waiting or running job enriched with repo slug, queue position,
    and blocked_reason. M2 emits these into Snapshot.queue (waiting only).
    Running jobs feed totals + per_repo counts but are NOT in queue.

    Field-name notes (vs M1's RawJob):
      - `job_id` (not `id`) so callers reading Snapshot don't confuse it
        with `task_id` / `repo_id` / `owner_id` in the same row. The
        wire JSON contract uses `"job_id"` for the same reason.
      - `job_name` (not `name`) for the same disambiguation: a row also
        carries `repo`, and `name` is ambiguous between them.
      - `task_id` is `int | None`. Forgejo emits `0` while a job is
        waiting and a real id once it goes running; M2 normalizes the
        waiting-sentinel to None so the type signals "not yet
        scheduled" rather than relying on an in-band sentinel.
      - `position` is `int` (not Optional) because queue holds waiting
        jobs only; every Job in queue has a 1-based position.
      - `status` is `Literal["waiting","running"]`. M2 only ever
        constructs Job from a RawJob whose normalized status is one of
        these two strings; unknown wire values would have already been
        bucketed by aggregate()'s totals/per_repo path without
        reaching Job construction.
    """

    job_id: int
    job_name: str
    status: Literal["waiting", "running"]
    repo: str  # owner/repo or repo#<id> fallback
    repo_id: int
    owner_id: int
    runs_on: tuple[str, ...]
    needs: tuple[str, ...]
    task_id: int | None  # None while waiting (Forgejo emits 0)
    attempt: int
    handle: str
    position: int  # 1-based across waiting jobs
    blocked_reason: str | None  # see vocabulary constants above


@dataclass(frozen=True)
class RepoBreakdown:
    """Per-repo running/waiting/total counts. Sorted by `repo` slug in
    Snapshot.per_repo.
    """

    repo: str
    repo_id: int
    running: int
    waiting: int
    total: int


@dataclass(frozen=True)
class Totals:
    """Instance-wide counts. Future scoped/filtered renderers may keep
    `totals` instance-wide per the PRD JSON contract.
    """

    running: int
    waiting: int
    total: int


@dataclass(frozen=True)
class Warning:  # noqa: A001 (intentional shadow within module; PRD-named)
    """Structured warning row. M2 currently emits only
    `code == "unschedulable_labels"` (the genuine stuck case). The model
    is structured so M3+ can add new codes additively without changing
    the JSON shape.

    Name `Warning` matches the PRD §Architecture-notes model list; it
    shadows the builtin within this module the same way ConnectionError
    does. Module-external callers reference it as `fj_queue.Warning`.
    """

    code: str
    job_id: int
    repo: str
    runs_on: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Snapshot:
    """Canonical aggregation object. Feeds all three renderers (M3..M5).

    `as_of` is the injected clock value (NOT datetime.now()), so the JSON
    snapshot tests in M3/M7 are byte-stable. `schema_version` is the
    integer the JSON contract advertises (bump on breaking change).
    """

    as_of: datetime
    host: str
    schema_version: int
    filter_repo: str | None
    filter_label: tuple[str, ...]
    runners: tuple[Runner, ...]
    totals: Totals
    per_repo: tuple[RepoBreakdown, ...]
    queue: tuple[Job, ...]
    schedulable_labels: tuple[str, ...]
    warnings: tuple[Warning, ...]
    # Per-pod runner CPU/memory from Prometheus (M-metrics). Passed
    # through unchanged by aggregate() (which stays pure / I/O-free); the
    # actual Prometheus fetch happens in _do_one_fetch. `runner_pods` is
    # empty and `metrics_error` carries the reason string when the fetch
    # failed or metrics are disabled.
    runner_pods: tuple[PodResource, ...] = ()
    metrics_error: str | None = None
    # NCPS cache activity (nix-cache.example.com), from the same Prometheus
    # source. None when metrics are disabled or the NCPS fetch failed
    # (the shared `metrics_error` carries the reason in the common case);
    # aggregate() passes it through unchanged (the fetch lives in
    # _do_one_fetch, keeping aggregate pure).
    ncps: NcpsStatus | None = None


# ---------------------------------------------------------------------------
# Internal helpers (scheduling math).
# ---------------------------------------------------------------------------


def _job_runs_on_satisfied_by_any(
    runs_on: tuple[str, ...],
    online_runner_label_sets: Iterable[frozenset[str]],
) -> bool:
    """Per-runner SUPERSET test (the source-verified rule).

    Returns True iff at least one online runner's labels are a superset
    of the job's runs_on. Empty runs_on is trivially a subset of every
    set, so any online runner satisfies it -> True. The all-offline
    case has no online_runner_label_sets, so this returns False
    naturally (no special-case needed).
    """
    runs_on_set = frozenset(runs_on)
    for labels in online_runner_label_sets:
        if runs_on_set.issubset(labels):
            return True
    return False


def _derive_blocked_reason(
    job: RawJob,
    online_runner_label_sets: list[frozenset[str]],
) -> str | None:
    """Map a RawJob to a blocked_reason string (or None for running).

    Priority: running -> None ; non-empty needs -> blocked_on_needs ;
    schedulable -> waiting_for_runner ; otherwise -> unschedulable.
    """
    if job.status == _STATUS_RUNNING:
        return None
    # Anything not "running" we treat as waiting for blocked_reason
    # purposes. The API only emits waiting+running, but if a future
    # version exposes blocked/cancelled/etc., we err on "show it in the
    # queue with a reason" rather than crashing.
    if job.needs:
        return BLOCKED_ON_NEEDS
    if _job_runs_on_satisfied_by_any(job.runs_on, online_runner_label_sets):
        return BLOCKED_WAITING_FOR_RUNNER
    return BLOCKED_UNSCHEDULABLE


# ---------------------------------------------------------------------------
# Public: aggregate().
# ---------------------------------------------------------------------------


def aggregate(
    runners: Iterable[Runner],
    jobs: Iterable[RawJob],
    repo_names: Mapping[int, str],
    now: datetime,
    *,
    host: str,
    filter_repo: str | None = None,
    filter_label: tuple[str, ...] = (),
    schema_version: int = 1,
    runner_pods: tuple[PodResource, ...] = (),
    metrics_error: str | None = None,
    ncps: NcpsStatus | None = None,
) -> Snapshot:
    """Build the canonical Snapshot from pre-fetched inputs.

    Pure: no I/O, no clock reads, no module state mutation. The caller
    owns the HTTP layer (M1 Client) and the clock; aggregate() is the
    deterministic core that M3 JSON, M4 renderers, and M7 fixtures all
    pin against.

    Arguments:
      runners:     full instance-wide runner list (all pages followed).
      jobs:        full waiting+running job list (the endpoint does
                   not paginate; we get the entire snapshot per poll).
      repo_names:  repo_id -> "owner/repo" map, pre-resolved via
                   Client.resolve_repo. Missing ids fall back to
                   `repo#<id>` so a stale map can never abort.
      now:         the snapshot's as_of timestamp. Injected so tests
                   are byte-stable; MUST be timezone-aware (UTC).
      host:        the Forgejo host string used in the JSON header.
      filter_repo: echoes the --repo scope decision (CLI in M6).
      filter_label: echoes the --label scope decision (CLI in M6).
      schema_version: integer version pinned at the wire layer.
      runner_pods: per-pod CPU/memory rows pre-fetched from Prometheus
                   (the I/O lives in _do_one_fetch; aggregate stays pure).
                   Empty when metrics are disabled or the fetch failed.
      metrics_error: the Prometheus failure reason string, or None on
                   success / when metrics are disabled.
      ncps: NCPS cache status pre-fetched from Prometheus, or None when
                   metrics are disabled or the NCPS fetch failed.
    """
    if now.tzinfo is None:
        # Defensive: a naive datetime would emit a non-RFC3339 string in
        # M3's JSON envelope. Surface the mistake at the boundary, not
        # in a downstream test failure.
        raise ValueError(
            "aggregate(now=...) MUST be timezone-aware (UTC); "
            "got a naive datetime"
        )

    runners_tuple: tuple[Runner, ...] = tuple(
        sorted(runners, key=lambda r: (r.name, r.id))
    )

    # Precompute online runner label sets once. Used by every waiting
    # job's blocked_reason derivation below.
    online_label_sets: list[frozenset[str]] = [
        frozenset(r.labels) for r in runners_tuple if is_online(r)
    ]

    # schedulable_labels (PRD JSON contract): union of ONLINE runners'
    # labels, sorted alphabetically.
    schedulable_labels = tuple(
        sorted({lbl for labels in online_label_sets for lbl in labels})
    )

    # Materialize jobs in a stable order (id ASC). Forgejo serves
    # id DESC; re-sorting is the source-verified FIFO approximation.
    jobs_list = sorted(jobs, key=lambda j: j.id)

    # Counts (instance-wide, even when filtered downstream).
    running = sum(1 for j in jobs_list if j.status == _STATUS_RUNNING)
    waiting = sum(1 for j in jobs_list if j.status == _STATUS_WAITING)
    # Anything not running/waiting still counts toward total but is not
    # broken into a separate bucket; future-proofs against unknown
    # statuses without crashing.
    total = len(jobs_list)
    totals = Totals(running=running, waiting=waiting, total=total)

    # Per-repo breakdown. Use repo_names map; fall back to repo#<id> for
    # missing ids. We aggregate counts under the resolved slug, which
    # means two ids that resolve to the same slug (shouldn't happen, but
    # defensive) merge into one row.
    per_repo_running: dict[str, int] = {}
    per_repo_waiting: dict[str, int] = {}
    per_repo_id: dict[str, int] = {}  # remember an id per slug
    for j in jobs_list:
        slug = repo_names.get(j.repo_id) or f"repo#{j.repo_id}"
        per_repo_id.setdefault(slug, j.repo_id)
        if j.status == _STATUS_RUNNING:
            per_repo_running[slug] = per_repo_running.get(slug, 0) + 1
        elif j.status == _STATUS_WAITING:
            per_repo_waiting[slug] = per_repo_waiting.get(slug, 0) + 1
        else:
            # Bucket unknown statuses under waiting for the per-repo
            # display (closer to "not running"); totals.total is still
            # the truth.
            per_repo_waiting[slug] = per_repo_waiting.get(slug, 0) + 1
    per_repo_slugs = sorted(set(per_repo_running) | set(per_repo_waiting))
    per_repo: tuple[RepoBreakdown, ...] = tuple(
        RepoBreakdown(
            repo=slug,
            repo_id=per_repo_id[slug],
            running=per_repo_running.get(slug, 0),
            waiting=per_repo_waiting.get(slug, 0),
            total=(
                per_repo_running.get(slug, 0) + per_repo_waiting.get(slug, 0)
            ),
        )
        for slug in per_repo_slugs
    )

    # Build the waiting-only queue with position + blocked_reason.
    waiting_jobs = [j for j in jobs_list if j.status == _STATUS_WAITING]
    queue_list: list[Job] = []
    warnings_list: list[Warning] = []
    for idx, j in enumerate(waiting_jobs, start=1):
        slug = repo_names.get(j.repo_id) or f"repo#{j.repo_id}"
        reason = _derive_blocked_reason(j, online_label_sets)
        queue_list.append(
            Job(
                job_id=j.id,
                job_name=j.name,
                # Cast to the Literal: aggregate filters by status string,
                # so anything reaching here is one of the two known values.
                status=j.status,  # type: ignore[arg-type]
                repo=slug,
                repo_id=j.repo_id,
                owner_id=j.owner_id,
                runs_on=j.runs_on,
                needs=j.needs,
                # Forgejo emits 0 while waiting; surface as None so the
                # type carries the "not yet scheduled" meaning instead
                # of an in-band sentinel.
                task_id=j.task_id if j.task_id else None,
                attempt=j.attempt,
                handle=j.handle,
                position=idx,
                blocked_reason=reason,
            )
        )
        if reason == BLOCKED_UNSCHEDULABLE:
            warnings_list.append(
                Warning(
                    code=WARN_UNSCHEDULABLE_LABELS,
                    job_id=j.id,
                    repo=slug,
                    runs_on=j.runs_on,
                    message=(
                        f"No online runner can satisfy runs_on: "
                        f"{list(j.runs_on)}"
                    ),
                )
            )

    warnings_tuple = tuple(sorted(warnings_list, key=lambda w: w.job_id))

    return Snapshot(
        as_of=now,
        host=host,
        schema_version=schema_version,
        filter_repo=filter_repo,
        filter_label=filter_label,
        runners=runners_tuple,
        totals=totals,
        per_repo=per_repo,
        queue=tuple(queue_list),
        schedulable_labels=schedulable_labels,
        warnings=warnings_tuple,
        runner_pods=tuple(runner_pods),
        metrics_error=metrics_error,
        ncps=ncps,
    )


# ===========================================================================
# M3 -- JSON contract + error envelope + schema.
#
# The agent interface. Pure transformation from Snapshot (M2) to a wire
# JSON document that:
#   * matches the PRD §JSON-contract example field-for-field;
#   * is byte-stable under a frozen clock (json.dumps with sort_keys=True
#     + an injected `as_of`);
#   * validates against `schema/fj-queue.v1.json` (loaded from disk via
#     load_schema(); also returned by render_schema() for the --schema
#     CLI flag M6 wires up);
#   * is additive-only for forward compatibility (no
#     additionalProperties:false anywhere in the schema; breaking
#     changes bump schema_version).
#
# Field-name mapping at THIS layer (internal -> wire):
#   Snapshot.as_of           -> "as_of" (RFC3339 UTC, 'Z' suffix)
#   Snapshot.filter_repo     -> "filter.repo"   (str | null)
#   Snapshot.filter_label    -> "filter.label"  (list[str] | null; null
#                              when empty tuple, to match PRD example)
#   Runner.status            -> "status"
#   is_online(runner)        -> "online" (derived bool)
#   Job.job_id               -> "job_id"
#   Job.job_name             -> "job_name"
#   Job.task_id              -> "task_id" (null when None)
#   (everything else passes through with the same name as on the model)
#
# Error envelope shape (stdout on failure; human diagnostics go to stderr):
#   {
#     "schema_version": 1,
#     "error": {
#       "code": "<usage|auth|connection|schema_drift|internal>",
#       "message": "<short human-readable>",
#       "host": "<host string from Config>"
#     }
#   }
# Exit code is exc.exit_code (PRD §error contract typed codes: 2/3/4/5).
# ===========================================================================

import json as _json
from pathlib import Path as _Path


# Default JSON-Schema file location (sibling of this module).
_SCHEMA_PATH = _Path(__file__).resolve().parent / "schema" / "fj-queue.v1.json"


def _rfc3339_utc_z(dt: datetime) -> str:
    """Format a datetime as second-precision RFC3339 with 'Z' suffix.

    Matches the PRD JSON-contract example exactly (`2026-05-26T14:03:11Z`).
    Requires a UTC-aware datetime; M2's aggregate() already enforces that
    at the boundary, but we check again here so a hand-rolled error-path
    timestamp cannot smuggle a naive value through.
    """
    if dt.tzinfo is None:
        raise ValueError("rfc3339_utc_z requires a timezone-aware datetime")
    # Normalize to UTC if a non-UTC tzinfo was passed.
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _runner_to_dict(r: Runner) -> dict:
    """Wire shape for a runner row. Adds the derived `online` boolean."""
    return {
        "id": r.id,
        "name": r.name,
        "status": r.status,
        "online": is_online(r),
        "version": r.version,
        "labels": list(r.labels),
        "ephemeral": r.ephemeral,
    }


def _job_to_dict(j: Job) -> dict:
    return {
        "job_id": j.job_id,
        "task_id": j.task_id,  # already int | None
        "position": j.position,
        "status": j.status,
        "repo": j.repo,
        "repo_id": j.repo_id,
        "owner_id": j.owner_id,
        "job_name": j.job_name,
        "attempt": j.attempt,
        "runs_on": list(j.runs_on),
        "needs": list(j.needs),
        "blocked_reason": j.blocked_reason,  # already str | None
    }


def _pod_to_dict(p: PodResource) -> dict:
    """Wire shape for a runner pod resource row. Raw numbers (NOT
    human-formatted MiB/cores): agents parse this; the rich/plain
    renderers do the MiB/decimal formatting.
    """
    return {
        "pod": p.pod,
        "node": p.node,
        "cpu_cores": p.cpu_cores,
        "memory_bytes": p.memory_bytes,
        # Configured limits, raw numbers; null when no limit is set.
        "cpu_limit_cores": p.cpu_limit_cores,
        "memory_limit_bytes": p.memory_limit_bytes,
    }


def _ncps_to_dict(n: "NcpsStatus | None") -> dict | None:
    """Wire shape for NCPS status. null when disabled/failed; raw numbers
    otherwise (renderers do the req/s, MiB/s, miss/s formatting).
    """
    if n is None:
        return None
    return {
        "active": n.active,
        "requests_per_sec": n.requests_per_sec,
        "inflight": n.inflight,
        "upstream_per_sec": n.upstream_per_sec,
        "bytes_per_sec": n.bytes_per_sec,
    }


def _per_repo_to_dict(pr: RepoBreakdown) -> dict:
    return {
        "repo": pr.repo,
        "repo_id": pr.repo_id,
        "running": pr.running,
        "waiting": pr.waiting,
        "total": pr.total,
    }


def _warning_to_dict(w: Warning) -> dict:
    return {
        "code": w.code,
        "job_id": w.job_id,
        "repo": w.repo,
        "runs_on": list(w.runs_on),
        "message": w.message,
    }


def to_dict(snap: Snapshot) -> dict:
    """Explicit Snapshot -> wire-dict serializer.

    Deliberately NOT `dataclasses.asdict(snap)`: the architecture
    invariant from the PRD is that the wire schema is decoupled from
    internal field names, so internal renames (e.g. `Job.id` ->
    `Job.job_id` in M2's alignment) cannot accidentally break agents.
    Every wire field comes from an explicit mapping above.
    """
    # filter_label: empty tuple -> null on the wire; non-empty -> list.
    # Matches PRD example which shows `"label": null` when not scoped.
    filter_label_wire: list[str] | None
    if snap.filter_label:
        filter_label_wire = list(snap.filter_label)
    else:
        filter_label_wire = None

    return {
        "schema_version": snap.schema_version,
        "as_of": _rfc3339_utc_z(snap.as_of),
        "host": snap.host,
        "filter": {
            "repo": snap.filter_repo,
            "label": filter_label_wire,
        },
        "schedulable_labels": list(snap.schedulable_labels),
        "runners": [_runner_to_dict(r) for r in snap.runners],
        "totals": {
            "running": snap.totals.running,
            "waiting": snap.totals.waiting,
            "total": snap.totals.total,
        },
        "per_repo": [_per_repo_to_dict(pr) for pr in snap.per_repo],
        "queue": [_job_to_dict(j) for j in snap.queue],
        "warnings": [_warning_to_dict(w) for w in snap.warnings],
        # Per-pod runner CPU/memory (additive v1.x keys; raw numbers).
        # Always present for contract stability, even when empty. On a
        # Prometheus failure `metrics.error` carries the reason and
        # `runner_pods` is [].
        "runner_pods": [_pod_to_dict(p) for p in snap.runner_pods],
        "metrics": {
            "source": "prometheus",
            "error": snap.metrics_error,
            "rate_window": _PROM_RATE_WINDOW,
        },
        # NCPS cache status (additive v1.x key; raw numbers). null when
        # metrics are disabled or the NCPS fetch failed.
        "ncps": _ncps_to_dict(snap.ncps),
    }


def render_json(snap: Snapshot, *, indent: int | None = 2) -> str:
    """Serialize Snapshot to a JSON string.

    `sort_keys=True` is the byte-stable contract anchor: the same
    Snapshot produces the same byte stream across runs and across
    Python implementations (CPython dict ordering is insertion-stable
    today, but we do NOT rely on that). Default `indent=2` for human
    readability; pass `indent=None` for a single-line agent stream.
    """
    return _json.dumps(
        to_dict(snap),
        sort_keys=True,
        indent=indent,
        ensure_ascii=False,
    )


def _scrub_token(message: str, token: str | None) -> str:
    """Mask literal occurrences of `token` in `message` with `***`.

    Defensive helper used by every path that surfaces an exception
    message to the operator (JSON error envelope, stderr diagnostic,
    Rich mid-loop error panel). Centralized so all three paths apply
    the same masking and a future leak in one of them cannot diverge
    from the others. No-op when `token` is falsy.
    """
    if not token:
        return message
    return message.replace(token, "***")


def render_json_error(
    exc: FjQueueError | Exception,
    *,
    host: str,
    schema_version: int = 1,
    indent: int | None = 2,
    scrub_token: str | None = None,
) -> str:
    """Serialize a typed error to the stdout JSON error envelope.

    Anything that is not an FjQueueError is wrapped as a generic
    `internal` error (exit 1) so even an unexpected crash still emits
    parseable JSON on stdout. Human diagnostics (stack trace) belong on
    stderr, not in this envelope.

    Token-leak defense: PRD §error contract demands "human-readable,
    NEVER contains the token". The exception messages M1/M2/M3 raise
    today do not include the token (M1's _get no longer bakes the
    response body into the message; only status code + URL path).
    `scrub_token` is opt-in defense for any future code path that
    might surface a token-containing string into an exception
    message: M5/M6 callers should pass `Config.token` so any literal
    occurrence is masked to `***` before serialization. Safe to
    omit; pass it whenever a Config object is in scope. Mirrors the
    `_scrub_token` pass applied symmetrically on the stderr and Rich
    error-panel paths.
    """
    if isinstance(exc, FjQueueError):
        code = exc.error_code
    else:
        code = "internal"
    message = _scrub_token(str(exc) or exc.__class__.__name__, scrub_token)
    return _json.dumps(
        {
            "schema_version": schema_version,
            "error": {
                "code": code,
                "message": message,
                "host": host,
            },
        },
        sort_keys=True,
        indent=indent,
        ensure_ascii=False,
    )


def load_schema(path: _Path | None = None) -> dict:
    """Load and parse the JSON Schema. Default location is the sibling
    `schema/fj-queue.v1.json` file. Cached at the OS-page level by
    standard read-cache; we do not memoize here because tests sometimes
    override the path.
    """
    target = path or _SCHEMA_PATH
    return _json.loads(target.read_text(encoding="utf-8"))


def render_schema(*, indent: int | None = 2) -> str:
    """Return the JSON Schema as a JSON string. Wired into the M6
    `--schema` CLI flag.
    """
    return _json.dumps(load_schema(), sort_keys=True, indent=indent)


def exit_code_for(exc: BaseException) -> int:
    """Map an exception to the PRD §error-contract typed exit code.

    Unknown exception types fall through as 1 (generic non-zero), never
    as 0; the contract says only a successful render returns 0.
    """
    if isinstance(exc, FjQueueError):
        return exc.exit_code
    return 1


# ===========================================================================
# M4 -- Plain + Rich rendering.
#
# Two renderers, both consuming Snapshot ONLY (no recomputation, no I/O,
# no clock). render_plain returns a no-color line-oriented string;
# render_rich returns a `rich.console.Renderable` suitable for both
# Console.print() and rich.live.Live(screen=True) (the M5 watch loop).
#
# Plain-rendering design:
#   * pipe-friendly + greppable + non-TTY safe (no ANSI escapes ever)
#   * no Unicode box-drawing -- only ASCII so grep/sed/awk/cut behave
#   * stable section ordering (header, totals, runners, runner
#     resources per pod, NCPS status, per_repo, queue, warnings) so
#     consumers can pin section anchors
#   * each row prints all relevant identity fields (job_id, repo) so a
#     line is independently meaningful when filtered by grep
#
# Rich-rendering design:
#   * top-level `rich.console.Group` of: header text, totals strip,
#     runner Table, per_repo Table, queue Table, warning rows
#   * `rich` is imported lazily so the agent (--format json) path
#     never pays the rich import cost
#   * Rich's Console honors NO_COLOR automatically; we do not need to
#     do anything explicit for the env var
#   * No mutation of Snapshot; ordering already pinned at the
#     aggregation layer (M2 contract).
# ===========================================================================


def _format_str_list(items: tuple[str, ...] | list[str]) -> str:
    """Render a tuple/list of strings as `[a, b, c]` or `[]`.

    Used in plain output where we want a compact, grep-friendly form
    that mirrors the JSON wire format (which agents are used to). Not
    used for rich tables, which render lists as comma-joined cells.
    """
    if not items:
        return "[]"
    return "[" + ", ".join(items) + "]"


def _runner_online_count(snap: "Snapshot") -> int:
    return sum(1 for r in snap.runners if is_online(r))


def _human_bytes(memory_bytes: int) -> str:
    """Bytes -> human MiB/GiB for display (JSON keeps raw bytes).

    Picks MiB below 1024 MiB, GiB at/above it (1 decimal). So usage
    ~728 MiB stays `728 MiB`; a ~17.4 GiB limit reads `17.4 GiB`.
    """
    mib = memory_bytes / 1024 / 1024
    if mib >= 1024:
        return f"{memory_bytes / 1024 / 1024 / 1024:.1f} GiB"
    return f"{round(mib)} MiB"


def _fmt_cpu_cell(usage_cores: float, limit_cores: float | None, *, dash: str) -> str:
    """`usage / limit` cores, 3 decimals; `dash` denominator when None."""
    limit = f"{limit_cores:.3f}" if limit_cores is not None else dash
    return f"{usage_cores:.3f} / {limit}"


def _fmt_mem_cell(usage_bytes: int, limit_bytes: int | None, *, dash: str) -> str:
    """`usage / limit` human bytes; `dash` denominator when None."""
    limit = _human_bytes(limit_bytes) if limit_bytes is not None else dash
    return f"{_human_bytes(usage_bytes)} / {limit}"


def _ncps_summary(snap: "Snapshot") -> tuple[str, str]:
    """(text, kind) for the NCPS status line. kind is one of
    active/idle/disabled/unavailable, used by the rich renderer to pick a
    style. Shares the metrics_error path (no second error field): a
    disabled run is metrics_error == METRICS_DISABLED; a None ncps with no
    populated reason falls back to "no data".
    """
    if snap.metrics_error == METRICS_DISABLED:
        return "disabled (--no-metrics)", "disabled"
    if snap.ncps is None:
        # No status and no failure reason: NCPS just wasn't observed.
        # Read as "no data", distinct from a real "unavailable (<reason>)".
        if snap.metrics_error is None:
            return "no data", "no_data"
        return f"unavailable ({snap.metrics_error})", "unavailable"
    n = snap.ncps
    if n.active:
        return (
            f"active ({n.requests_per_sec:.1f} req/s, "
            f"{_human_bytes(int(n.bytes_per_sec))}/s, "
            f"{n.upstream_per_sec:.1f} miss/s)",
            "active",
        )
    return "idle", "idle"


def render_plain(snap: "Snapshot") -> str:
    """Render Snapshot as a no-color, line-oriented text block.

    Output is deterministic given a deterministic Snapshot (which M2
    guarantees with sorted tuples + injected clock). Safe to pipe; no
    ANSI escapes, no Unicode box-drawing, ASCII only.
    """
    lines: list[str] = []
    online = _runner_online_count(snap)
    n_runners = len(snap.runners)

    # Header.
    as_of = _rfc3339_utc_z(snap.as_of)
    lines.append(f"fj-queue snapshot  as_of={as_of}  host={snap.host}")
    f_repo = snap.filter_repo if snap.filter_repo else "-"
    f_label = (
        _format_str_list(snap.filter_label) if snap.filter_label else "-"
    )
    lines.append(f"  filter: repo={f_repo}  label={f_label}")
    lines.append(
        f"  schedulable_labels: {_format_str_list(snap.schedulable_labels)}"
    )
    lines.append("")

    # Totals (always present; PRD success-criterion "exit 0 on empty").
    t = snap.totals
    lines.append(
        f"TOTALS: running={t.running}  waiting={t.waiting}  total={t.total}"
    )
    lines.append("")

    # Runners.
    lines.append(f"RUNNERS ({online} online of {n_runners}):")
    if not snap.runners:
        lines.append("  (none)")
    else:
        for r in snap.runners:
            on_marker = "online " if is_online(r) else "offline"
            lines.append(
                f"  {r.name:<24} {r.status:<8} {on_marker}  "
                f"{r.version:<10} labels={_format_str_list(r.labels)}"
                f"  ephemeral={r.ephemeral}"
            )
    lines.append("")

    # Runner resources (per pod, from Prometheus). Combined containers.
    if snap.metrics_error == METRICS_DISABLED:
        # Intentional toggle, not a failure: read as such.
        lines.append("RUNNER RESOURCES: disabled (--no-metrics)")
    elif snap.metrics_error is not None:
        lines.append(
            f"RUNNER RESOURCES: unavailable ({snap.metrics_error})"
        )
    else:
        lines.append(
            f"RUNNER RESOURCES (per pod, {len(snap.runner_pods)}):"
        )
        if not snap.runner_pods:
            lines.append("  (none)")
        else:
            for p in snap.runner_pods:
                # Plain stays ASCII-only: a None limit renders as `-`.
                cpu_cell = _fmt_cpu_cell(p.cpu_cores, p.cpu_limit_cores, dash="-")
                mem_cell = _fmt_mem_cell(p.memory_bytes, p.memory_limit_bytes, dash="-")
                lines.append(
                    f"  {p.pod:<40} node={p.node:<14} "
                    f"cpu={cpu_cell}  mem={mem_cell}"
                )
    lines.append("")

    # NCPS cache status (one line). Same metrics_error path as above.
    ncps_text, _ = _ncps_summary(snap)
    lines.append(f"NCPS: {ncps_text}")
    lines.append("")

    # Per-repo.
    lines.append(f"PER REPO ({len(snap.per_repo)} repos):")
    if not snap.per_repo:
        lines.append("  (none)")
    else:
        for pr in snap.per_repo:
            lines.append(
                f"  {pr.repo:<32}  "
                f"running={pr.running}  waiting={pr.waiting}  "
                f"total={pr.total}"
            )
    lines.append("")

    # Queue (waiting only).
    lines.append(f"QUEUE ({len(snap.queue)} waiting):")
    if not snap.queue:
        lines.append("  (empty)")
    else:
        for j in snap.queue:
            reason = j.blocked_reason or "-"
            lines.append(
                f"  #{j.position:<3} "
                f"[{reason:<19}] "
                f"job_id={j.job_id}  "
                f"repo={j.repo}  "
                f"name=\"{j.job_name}\""
            )
            # Indented secondary line so grep on `job_id=` still hits.
            lines.append(
                f"        attempt={j.attempt}  "
                f"runs_on={_format_str_list(j.runs_on)}  "
                f"needs={_format_str_list(j.needs)}"
            )
    lines.append("")

    # Warnings.
    lines.append(f"WARNINGS ({len(snap.warnings)}):")
    if not snap.warnings:
        lines.append("  (none)")
    else:
        for w in snap.warnings:
            lines.append(
                f"  {w.code}  job_id={w.job_id}  repo={w.repo}  "
                f"runs_on={_format_str_list(w.runs_on)}"
            )
            lines.append(f"    -> {w.message}")

    return "\n".join(lines)


def render_rich(snap: "Snapshot"):
    """Build a Rich Renderable for the snapshot.

    Returns a `rich.console.Group` containing a header, totals strip,
    runner table, per-repo table, queue table, and warning rows. The
    `rich` package is imported lazily so callers using only the JSON
    path do not pay the import cost.

    Color choices use Rich's named theme tokens (no inline hex), so
    the output respects the user's terminal palette and Rich's
    NO_COLOR handling kicks in automatically.
    """
    # Lazy import: --format json must not pay this cost.
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    sections: list = []

    # --- Header panel: as_of + host + totals + schedulable labels ----
    online = _runner_online_count(snap)
    n_runners = len(snap.runners)
    t = snap.totals
    header_text = Text()
    header_text.append("fj-queue ", style="bold")
    header_text.append(f"@ {snap.host}", style="cyan")
    header_text.append(f"   as_of={_rfc3339_utc_z(snap.as_of)}\n", style="dim")
    header_text.append("runners: ", style="bold")
    header_text.append(f"{online} online / {n_runners}", style="green" if online else "red")
    header_text.append("    totals: ", style="bold")
    header_text.append(f"running={t.running}  waiting={t.waiting}  total={t.total}")
    if snap.filter_repo or snap.filter_label:
        header_text.append("\nfilter: ", style="bold")
        header_text.append(
            f"repo={snap.filter_repo or '-'}  "
            f"label={_format_str_list(snap.filter_label) if snap.filter_label else '-'}",
            style="yellow",
        )
    if snap.schedulable_labels:
        header_text.append("\nschedulable_labels: ", style="bold")
        header_text.append(
            _format_str_list(snap.schedulable_labels), style="cyan"
        )
    sections.append(Panel(header_text, title="snapshot", expand=False))

    # --- Runners table -----------------------------------------------
    rt = Table(title="Runners", show_lines=False, expand=False)
    rt.add_column("name", style="bold")
    rt.add_column("status")
    rt.add_column("online", justify="center")
    rt.add_column("version", style="dim")
    rt.add_column("labels")
    rt.add_column("ephemeral", justify="center")
    if not snap.runners:
        rt.add_row("(none)", "", "", "", "", "")
    else:
        for r in snap.runners:
            online_cell = (
                Text("yes", style="green") if is_online(r)
                else Text("no", style="red")
            )
            # Three-way visual distinction (online operators want to see
            # active vs idle at a glance without breaking the
            # two-state online/offline semantics elsewhere):
            #   active  -> bright green
            #   idle    -> dim green (still "online", just unloaded)
            #   offline -> red
            if r.status == "active":
                status_style = "green"
            elif r.status == "idle":
                status_style = "dim green"
            else:
                status_style = "red"
            rt.add_row(
                r.name,
                Text(r.status, style=status_style),
                online_cell,
                r.version,
                ", ".join(r.labels),
                "yes" if r.ephemeral else "no",
            )
    sections.append(rt)

    # --- Runner resources (per pod) ----------------------------------
    # Combined-container CPU/memory from Prometheus. On a metrics failure
    # render a dim "unavailable" line instead of a table so the dashboard
    # degrades gracefully rather than showing an empty/blank section.
    if snap.metrics_error == METRICS_DISABLED:
        # Intentional toggle, not a failure: read as such.
        sections.append(
            Text("Runner resources: disabled (--no-metrics)", style="dim")
        )
    elif snap.metrics_error is not None:
        sections.append(
            Text(
                f"Runner resources: unavailable ({snap.metrics_error})",
                style="dim",
            )
        )
    else:
        mt = Table(
            title="Runner resources (per pod)", show_lines=False, expand=False
        )
        mt.add_column("pod", style="bold")
        mt.add_column("node")
        mt.add_column("cpu (cores)", justify="right", style="green")
        mt.add_column("mem (usage / limit)", justify="right", style="cyan")
        if not snap.runner_pods:
            mt.add_row("(none)", "", "", "")
        else:
            for p in snap.runner_pods:
                # Rich uses the literal em dash for a None limit
                # denominator (the addendum's spec; plain stays ASCII).
                mt.add_row(
                    p.pod,
                    p.node,
                    _fmt_cpu_cell(p.cpu_cores, p.cpu_limit_cores, dash="—"),
                    _fmt_mem_cell(p.memory_bytes, p.memory_limit_bytes, dash="—"),
                )
        sections.append(mt)

    # --- NCPS cache status (one line) --------------------------------
    # active -> green, idle/disabled/unavailable -> dim. Same
    # metrics_error path as the runner-resources section.
    ncps_text, ncps_kind = _ncps_summary(snap)
    sections.append(
        Text(
            f"NCPS: {ncps_text}",
            style="green" if ncps_kind == "active" else "dim",
        )
    )

    # --- Per-repo table ----------------------------------------------
    pt = Table(title="Per repo", show_lines=False, expand=False)
    pt.add_column("repo", style="bold")
    pt.add_column("running", justify="right", style="green")
    pt.add_column("waiting", justify="right", style="yellow")
    pt.add_column("total", justify="right")
    if not snap.per_repo:
        pt.add_row("(none)", "", "", "")
    else:
        for pr in snap.per_repo:
            pt.add_row(
                pr.repo,
                str(pr.running),
                str(pr.waiting),
                str(pr.total),
            )
    sections.append(pt)

    # --- Queue table -------------------------------------------------
    qt = Table(title="Queue (waiting, FIFO approx)", show_lines=False, expand=False)
    qt.add_column("#", justify="right", style="dim")
    qt.add_column("job_id", justify="right")
    qt.add_column("repo", style="bold")
    qt.add_column("job_name")
    qt.add_column("attempt", justify="right")
    qt.add_column("runs_on")
    qt.add_column("needs")
    qt.add_column("blocked_reason")
    if not snap.queue:
        qt.add_row("", "", "(empty)", "", "", "", "", "")
    else:
        for j in snap.queue:
            reason_style = {
                "unschedulable": "red bold",
                "blocked_on_needs": "yellow",
                "waiting_for_runner": "cyan",
            }.get(j.blocked_reason or "", "")
            reason_cell = (
                Text(j.blocked_reason, style=reason_style)
                if j.blocked_reason else Text("-", style="dim")
            )
            attempt_str = str(j.attempt)
            if j.attempt > 1:
                attempt_str = f"[red]{attempt_str} (rerun)[/red]"
            qt.add_row(
                str(j.position),
                str(j.job_id),
                j.repo,
                j.job_name,
                attempt_str,
                ", ".join(j.runs_on) if j.runs_on else "-",
                ", ".join(j.needs) if j.needs else "-",
                reason_cell,
            )
    sections.append(qt)

    # --- Warnings ----------------------------------------------------
    if snap.warnings:
        wt = Table(title="Warnings", show_lines=False, expand=False, border_style="red")
        wt.add_column("code", style="red bold")
        wt.add_column("job_id", justify="right")
        wt.add_column("repo")
        wt.add_column("runs_on")
        wt.add_column("message")
        for w in snap.warnings:
            wt.add_row(
                w.code,
                str(w.job_id),
                w.repo,
                _format_str_list(w.runs_on),
                w.message,
            )
        sections.append(wt)

    return Group(*sections)


# ===========================================================================
# M5 -- Live watch mode.
#
# Sync poll loop wrapped in `rich.live.Live(screen=True)` so the terminal
# refreshes in place with no scrollback spill. The PRD pins this error
# taxonomy hard (source-verified against the agent UX), do NOT widen:
#
#   * ConnectionError (timeout / 5xx / 429 / refused / 4xx-other):
#       transient by assumption. Render LAST-GOOD data plus a
#       `STALE (last good HH:MM:SS)` marker, keep ticking. The
#       first-tick variant (no last-good yet) escalates to exit so
#       the operator doesn't stare at an empty dashboard.
#
#   * AuthError (401 / 403):
#       definitive. Don't spin on a bad token. Loop stops, the
#       exception bubbles to the CLI for typed exit (exit=3).
#
#   * SchemaDrift:
#       definitive. Forgejo's response shape is outside what M1
#       knows how to parse; retrying won't fix it. Loop stops
#       (exit=5).
#
#   * KeyboardInterrupt:
#       clean teardown via Live's context manager + Client.close().
#       Return exit=0 (the operator deliberately stopped a working
#       loop; that's not a failure).
#
# Architecture: aggregate() is pure (M2), renderers are pure (M4), so
# the watch loop is the only place where I/O, clock, and sleep meet.
# All three are injectable for tests; the brief's
# `[good, good, timeout, good]` scripted-source test fakes the client
# AND the sleep AND the clock, drives N ticks, and inspects an
# on_frame() callback to assert per-frame state.
# ===========================================================================


def _resolve_repos_for_jobs(
    client: "Client",
    jobs: "Iterable[RawJob]",
) -> dict[int, str]:
    """Build the repo_id -> slug map a Snapshot needs.

    Each unique repo_id resolves via Client.resolve_repo, which has the
    per-process cache + `repo#<id>` fallback on 404/403/timeout. Reused
    by the watch loop here and by M6's --once path. Not a hot loop --
    cache keeps steady-state cost O(unique repos), not O(jobs * ticks).
    """
    repo_names: dict[int, str] = {}
    for j in jobs:
        if j.repo_id not in repo_names:
            repo_names[j.repo_id] = client.resolve_repo(j.repo_id)
    return repo_names


def _format_stale_marker(last_good_at: datetime, interval: float) -> str:
    """`STALE (last good HH:MM:SS UTC, retrying every Ns)` per PRD §M5
    + M5 expanded brief.

    Hour:minute:second in UTC (the same TZ as `as_of`); operators
    reading the dashboard see a wall-clock UTC marker matching the
    rest of the snapshot. The `retrying every Ns` suffix carries the
    `--interval` so the operator knows how often we are re-attempting
    without having to remember what they typed. `g` format trims
    `.0` (`2.0` -> `2`) but preserves fractional values (`2.5` ->
    `2.5`).
    """
    hms = last_good_at.astimezone(timezone.utc).strftime("%H:%M:%S")
    return f"STALE (last good {hms} UTC, retrying every {interval:g}s)"


def _build_error_renderable(
    exc: BaseException,
    *,
    title: str,
    headline: str,
    scrub_token: str | None = None,
):
    """One-frame error panel rendered to Live before the loop exits on
    a DEFINITIVE error (AuthError or SchemaDrift). Mid-loop only;
    first-tick failures bail without rendering anything to Live (PRD:
    no empty/error dashboard on the first tick).

    `scrub_token` (the active Config.token, if any) is masked from the
    rendered exception message via `_scrub_token`. Symmetric with the
    JSON envelope and the stderr diagnostic so a future leak in
    `str(exc)` cannot escape via this path while the other two scrub.
    """
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    body.append(headline + "\n", style="bold red")
    body.append(
        _scrub_token(str(exc) or exc.__class__.__name__, scrub_token),
        style="red",
    )
    body.append("\n\nfj-queue is stopping.", style="dim")
    return Panel(body, title=title, border_style="red", expand=False)


def _watch_renderable(
    snap: "Snapshot",
    stale_marker: str | None,
    *,
    format_mode: str = "rich",
):
    """Compose a frame: M4 renderer output, with a stale banner on top
    when present.

    `format_mode="rich"` returns a rich.console.Group (banner +
    render_rich output). `format_mode="plain"` returns a Rich Text
    wrapping `render_plain` output verbatim (no markup interpretation;
    the plain renderer's grep-safe layout reaches Live unchanged), with
    the stale marker as a leading line.
    """
    if format_mode == "plain":
        from rich.text import Text
        plain = render_plain(snap)
        if stale_marker is None:
            return Text(plain, no_wrap=False)
        return Text(stale_marker + "\n" + plain, no_wrap=False)

    base = render_rich(snap)
    if stale_marker is None:
        return base
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    banner = Panel(
        Text(stale_marker, style="bold yellow"),
        border_style="yellow",
        expand=False,
    )
    return Group(banner, base)


def _job_matches_filters(
    job: "Job",
    repo: str | None,
    labels: tuple[str, ...],
) -> bool:
    """Client-side --repo / --label scoping predicate operating on a
    post-aggregate Job record (repo slug + runs_on already resolved).

    --repo: the job's resolved slug must equal `repo`.
    --label: SUBSET semantics per PRD §Scheduling semantics -- the job
             matches iff every requested label is in the job's runs_on
             ("jobs whose runs_on includes at least these labels"). This
             mirrors the server-side `?labels=` filter direction; we
             apply it client-side AFTER aggregate so totals / per_repo /
             runners / warnings stay instance-wide.
    """
    if repo is not None and job.repo != repo:
        return False
    if labels and not set(labels).issubset(set(job.runs_on)):
        return False
    return True


def _scope_snapshot_queue(
    snap: "Snapshot",
    *,
    filter_repo: str | None,
    filter_label: tuple[str, ...],
) -> "Snapshot":
    """Apply --repo / --label scoping to `Snapshot.queue` ONLY.

    Per PRD §JSON contract (~line 94) and §Progress checkpoint M6
    (line 159): the filter scopes the queue array; totals, per_repo,
    runners, warnings, and schedulable_labels stay instance-wide.
    Surviving queue entries keep their GLOBAL position (1-based across
    the unfiltered waiting set), so positions may have gaps (e.g.
    [3, 7, 12]). filter_repo / filter_label are already echoed by
    aggregate(), so we return the original snap when no filter is
    requested.
    """
    if filter_repo is None and not filter_label:
        return snap
    scoped_queue = tuple(
        j for j in snap.queue
        if _job_matches_filters(j, filter_repo, filter_label)
    )
    return replace(snap, queue=scoped_queue)


def _do_one_fetch(
    client: "Client",
    host: str,
    now: datetime,
    *,
    filter_repo: str | None = None,
    filter_label: tuple[str, ...] = (),
    config: "Config | None" = None,
) -> "Snapshot":
    """One snapshot's worth of fetch + aggregate. Pure failure
    semantics: any exception bubbles untouched so the loop can do
    the retry/stop classification.

    Filter scoping (PRD §JSON contract ~line 94, §Progress checkpoint
    M6 line 159): aggregate runs over the FULL unfiltered jobs/runners
    list, so totals / per_repo / runners / warnings /
    schedulable_labels reflect the entire instance. AFTER aggregate,
    `--repo` / `--label` scope `Snapshot.queue` only; surviving entries
    keep their global `position` (gaps are intentional). The filter
    values are echoed into `Snapshot.filter` for the JSON contract.
    """
    runners = client.fetch_runners()
    jobs = client.fetch_jobs()
    repo_names = _resolve_repos_for_jobs(client, jobs)

    # Per-pod runner CPU/memory from Prometheus. ISOLATED from the Forgejo
    # client above (separate httpx client, no auth). Always-on with
    # graceful degradation: fetch_runner_pods never raises, but the
    # try/except is belt-and-braces so a Prometheus problem can NEVER
    # abort a snapshot. Disabled via --no-metrics.
    runner_pods: tuple[PodResource, ...] = ()
    metrics_error: str | None = None
    ncps: NcpsStatus | None = None
    if config is not None:
        if config.metrics_enabled:
            try:
                runner_pods, metrics_error = fetch_runner_pods(
                    config.metrics_url,
                    namespace=config.metrics_namespace,
                    cluster=config.metrics_cluster,
                    timeout=config.metrics_timeout,
                )
            except Exception as e:  # noqa: BLE001 (never crash on metrics)
                runner_pods, metrics_error = (), f"metrics fetch failed: {e}"
            # NCPS status from the same isolated Prometheus client.
            # Belt-and-braces (fetch_ncps_status already never raises).
            try:
                ncps = fetch_ncps_status(
                    config.metrics_url, timeout=config.metrics_timeout
                )
            except Exception:  # noqa: BLE001 (never crash on metrics)
                ncps = None
        else:
            # --no-metrics: mark the section "disabled" so consumers can
            # tell it apart from a successful fetch that found zero pods
            # (which is runner_pods=[] with metrics_error=None).
            metrics_error = METRICS_DISABLED

    snap = aggregate(
        runners,
        jobs,
        repo_names,
        now,
        host=host,
        filter_repo=filter_repo,
        filter_label=tuple(filter_label),
        runner_pods=runner_pods,
        metrics_error=metrics_error,
        ncps=ncps,
    )
    return _scope_snapshot_queue(
        snap,
        filter_repo=filter_repo,
        filter_label=tuple(filter_label),
    )


def run_watch(
    config: Config,
    *,
    interval: float = 2.0,
    iterations: int | None = None,
    format_mode: str = "rich",
    filter_repo: str | None = None,
    filter_label: tuple[str, ...] = (),
    client: "Client | None" = None,
    clock=None,
    sleep_fn=None,
    console=None,
    on_frame=None,
    screen: bool = True,
    error_pause: float = 0.5,
) -> int:
    """Drive the live watch loop. Returns the process exit code, OR
    raises an FjQueueError that the M6 CLI maps via exit_code_for().

    PRD #61 §M5 contract, source-pinned by the expanded brief:

      * Long-lived Client across the whole session (connection pool
        reuse; never reopened per tick).
      * One pure `aggregate(...)` call per tick using `clock()`'s
        `now`. This is the ONLY place in the module that reads the
        clock or the network; M2/M3/M4 stay pure.
      * On `--format json` + `--watch`: json wins, watch is downgraded
        to once. M6 enforces this BEFORE calling run_watch; run_watch
        itself is rich/plain only.

    Error taxonomy (each branch has a dedicated test):
      AuthError (401/403) and SchemaDrift -- DEFINITIVE, fail loud:
        Neither is fixable by retry. AuthError = bad/expired token.
        SchemaDrift = a wire-shape break (Forgejo upgrade changed the
        API, or a cross-host next-link security refusal); PRD §Risks
        line 182 mandates "fail loud (typed schema_drift, exit 5)".
        Behavior is identical for both: if mid-loop, render a
        one-frame error panel via live.update(), sleep `error_pause`
        seconds so the operator sees it, then re-raise; if first-tick
        (no last-good yet), bail WITHOUT rendering anything to Live
        (PRD: no empty/error dashboard on the first tick). M6 maps
        AuthError -> exit 3, SchemaDrift -> exit 5.
      ConnectionError (timeout / 5xx / 429 / refused) -- TRANSIENT:
        Show last-good data + a STALE banner, keep ticking. Re-raise
        on first-tick only (no last-good to render). A deploy in
        progress overwhelmingly surfaces as a 5xx / connection-refused
        (ConnectionError), NOT a parseable-but-wrong-shape body, so
        the transient treatment is scoped to ConnectionError alone.
        M6 maps first-tick ConnectionError -> exit 4.
      KeyboardInterrupt:
        Clean exit, terminal restored by Live's __exit__. Return
        EXIT_INTERRUPTED (130; POSIX 128 + SIGINT=2). NOT EXIT_OK;
        agents scripting the watch path want to distinguish "operator
        stopped a working loop" from "loop completed iterations".

    Exit codes returned (NOT raised):
      EXIT_OK (0)           normal termination (iterations cap hit)
      EXIT_INTERRUPTED (130) operator pressed Ctrl-C
    Exit codes delivered by raising (M6 maps via exit_code_for):
      EXIT_AUTH (3)         AuthError (any tick; bad/expired token)
      EXIT_CONNECTION (4)   first-tick ConnectionError
      EXIT_SCHEMA_DRIFT (5) SchemaDrift (any tick; wire-shape break)

    TTY fallback: if `screen=True` but the rendering console isn't a
    real terminal (e.g. stdout piped, or tests pass a StringIO
    Console), Live's alternate-screen mode would corrupt output.
    Fall back to `screen=False` automatically and warn on stderr.
    """
    import sys as _sys
    import time as _time
    from rich.console import Console as _RichConsole
    from rich.live import Live as _RichLive

    if clock is None:
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    if sleep_fn is None:
        sleep_fn = _time.sleep
    if console is None:
        console = _RichConsole()

    # TTY fallback: alternate-screen mode is meaningless when stdout
    # is a pipe or a StringIO. Downgrade gracefully and warn.
    if screen and not getattr(console, "is_terminal", True):
        _sys.stderr.write(
            "fj-queue: stdout is not a TTY; "
            "downgrading watch from screen mode to inline\n"
        )
        screen = False

    owns_client = client is None
    if client is None:
        client = Client(config)

    last_good: Snapshot | None = None
    last_good_at: datetime | None = None
    tick_index = 0

    try:
        with _RichLive(
            console=console,
            screen=screen,
            # Lower than Rich's default 10 because we call live.update()
            # explicitly each tick; the auto-refresh rate only governs
            # spinner/animation repaints, which this dashboard doesn't
            # use. Gentler on SSH/mosh terminals.
            refresh_per_second=4,
            # No-op while screen=True (the alt-screen is unconditionally
            # torn down on exit, htop/top/less semantics). Kept explicit
            # for readability and so a future screen=False inline mode
            # doesn't accidentally erase the last rendered frame.
            transient=False,
        ) as live:
            while True:
                if iterations is not None and tick_index >= iterations:
                    return EXIT_OK
                tick_index += 1
                now = clock()
                stale_marker: str | None = None
                snap_to_render: Snapshot | None = None

                try:
                    snap = _do_one_fetch(
                        client,
                        config.host,
                        now,
                        filter_repo=filter_repo,
                        filter_label=filter_label,
                        config=config,
                    )
                except (AuthError, SchemaDrift) as exc:
                    # DEFINITIVE: bad token (401/403) or a wire-shape
                    # break (Forgejo upgrade / cross-host next-link).
                    # Neither is fixable by retry; PRD §Risks line 182
                    # mandates fail-loud for schema drift (exit 5).
                    if last_good is None:
                        # First-tick: PRD says do NOT render an
                        # empty/error dashboard. Bail straight.
                        raise
                    # Mid-loop: render a one-frame error panel so the
                    # operator sees WHY the dashboard stopped, give the
                    # panel time to display, then re-raise.
                    if isinstance(exc, AuthError):
                        title, headline, marker = (
                            "auth error",
                            "Authentication failed. Fix the token.",
                            "AUTH_ERROR",
                        )
                    else:
                        title, headline, marker = (
                            "schema drift",
                            "Schema drift: Forgejo response shape changed.",
                            "SCHEMA_DRIFT",
                        )
                    live.update(
                        _build_error_renderable(
                            exc,
                            title=title,
                            headline=headline,
                            scrub_token=config.token,
                        )
                    )
                    if on_frame is not None:
                        on_frame(tick_index, None, marker)
                    sleep_fn(error_pause)
                    raise
                except ConnectionError:
                    # TRANSIENT: timeout / 5xx / 429 / refused. Keep
                    # ticking on last-good with a STALE banner.
                    if last_good is None:
                        # First-tick failure: bail rather than show an
                        # empty dashboard. CLI maps to exit=4.
                        raise
                    # last_good_at is non-None whenever last_good is.
                    assert last_good_at is not None
                    stale_marker = _format_stale_marker(
                        last_good_at, interval
                    )
                    snap_to_render = last_good
                else:
                    last_good = snap
                    last_good_at = now
                    snap_to_render = snap

                live.update(
                    _watch_renderable(
                        snap_to_render,
                        stale_marker,
                        format_mode=format_mode,
                    )
                )
                if on_frame is not None:
                    on_frame(tick_index, snap_to_render, stale_marker)

                if iterations is not None and tick_index >= iterations:
                    return EXIT_OK
                sleep_fn(interval)
    except KeyboardInterrupt:
        # POSIX convention: 128 + signal number. SIGINT = 2 -> 130.
        # Agents scripting fj-queue distinguish operator-stop (130)
        # from normal completion (0) on the watch path.
        return EXIT_INTERRUPTED
    finally:
        if owns_client:
            client.close()


# ===========================================================================
# M6 -- CLI ergonomics + packaging (argparse, stdlib only).
#
# Wires every layer into a runnable program:
#   uv run fj_queue.py [--mode {watch,once}|--watch|--once]
#                      [--format {rich,plain,json}]
#                      [--interval N] [--host H] [--token T] [--timeout S]
#                      [--label L ...] [--repo owner/repo]
#                      [--schema] [--version] [--help]
#
# Dispatch rules (PRD §Run modes + §Output formats):
#   * --schema: print schema/fj-queue.v1.json, exit 0 (no token needed).
#   * --version / --help: argparse handles, exit 0.
#   * --format json: forces once; on --watch+json, warn to stderr and
#     proceed once (json wins). Success -> stdout is ONLY the JSON
#     document; failure -> JSON error envelope to stdout + diagnostic
#     to stderr. Typed exit codes throughout.
#   * default mode (no --mode/--watch/--once): watch at a TTY, once when
#     stdout is piped (avoids an infinite loop on `fj-queue > file`).
#   * --repo / --label: client-side scoping applied AFTER aggregate
#     (see _do_one_fetch / _scope_snapshot_queue); only Snapshot.queue
#     is scoped, totals / per_repo / runners / warnings /
#     schedulable_labels stay instance-wide (PRD line 159).
# ===========================================================================


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="fj-queue",
        description=(
            "Read-only Forgejo Actions runner & CI queue dashboard. "
            "Polls the admin Actions API and renders runner inventory, "
            "queue totals, per-repo backlog, FIFO-approximate queue "
            "order, and unschedulable-job warnings."
        ),
        epilog=(
            "Exit codes: 0 ok, 2 usage, 3 auth (401/403), 4 "
            "connection/timeout/5xx/429, 5 schema drift, 130 interrupted."
        ),
    )

    # Run mode: --mode and the --watch/--once aliases are mutually
    # exclusive and all write to `dest=mode`. Default None -> infer
    # from TTY at dispatch time.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--mode",
        choices=["watch", "once"],
        default=None,
        help="watch (live refresh) or once (single snapshot). "
        "Default: watch at a terminal, once when piped.",
    )
    mode_group.add_argument(
        "--watch",
        action="store_const",
        const="watch",
        dest="mode",
        help="alias for --mode watch",
    )
    mode_group.add_argument(
        "--once",
        action="store_const",
        const="once",
        dest="mode",
        help="alias for --mode once",
    )

    parser.add_argument(
        "--format",
        choices=["rich", "plain", "json"],
        default="rich",
        help="output format (default: rich). json forces --once.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="watch poll interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--host",
        default="git.example.com",
        help="Forgejo host (default: git.example.com)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="API token (overrides $FORGEJO_TOKEN). Needs admin scope.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-request timeout in seconds (default: 10.0)",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=None,
        metavar="LABEL",
        help="scope to jobs whose runs_on includes at least this label "
        "(repeatable; subset filter)",
    )
    parser.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="scope to a single repo slug",
    )
    parser.add_argument(
        "--no-metrics",
        dest="metrics",
        action="store_false",
        default=True,
        help="disable per-pod runner CPU/memory metrics (Prometheus). "
        "Metrics are on by default and degrade gracefully on failure.",
    )
    parser.add_argument(
        "--metrics-url",
        default=None,
        metavar="URL",
        help="Prometheus base URL for runner metrics "
        "(default: $FJ_QUEUE_METRICS_URL or https://prometheus.example.com)",
    )
    parser.add_argument(
        "--metrics-namespace",
        default="forgejo-runner",
        metavar="NS",
        help="Kubernetes namespace of the runner pods "
        "(default: forgejo-runner)",
    )
    parser.add_argument(
        "--metrics-cluster",
        choices=["auto", "green", "blue"],
        default="auto",
        help="filter runner pods by node color prefix for blue/green "
        "(default: auto = no filter)",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="print the JSON Schema (schema/fj-queue.v1.json) and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"fj-queue {__version__}",
    )
    return parser


def _resolve_mode(args, *, is_tty: bool, stderr) -> str:
    """Decide watch vs once. --format json always forces once (warning
    if --watch was explicit). Otherwise honor an explicit mode; with no
    explicit mode, watch at a TTY, once when piped.
    """
    if args.format == "json":
        if args.mode == "watch":
            stderr.write(
                "fj-queue: --format json forces --once; ignoring --watch\n"
            )
        return "once"
    if args.mode is not None:
        return args.mode
    return "watch" if is_tty else "once"


def _emit_error(exc, *, host: str, fmt: str, token: str | None, stdout, stderr) -> int:
    """Centralized error delivery. In json mode the error envelope goes
    to stdout (the agent parses it); a human-readable diagnostic always
    goes to stderr. Both paths run through `_scrub_token` so the token
    cannot leak via `str(exc)` on either stream (symmetric with the
    watch-loop Rich error panel). Returns the typed exit code.
    """
    msg = _scrub_token(str(exc) or exc.__class__.__name__, token)
    if fmt == "json":
        stdout.write(
            render_json_error(exc, host=host, scrub_token=token) + "\n"
        )
    stderr.write(f"fj-queue: {msg}\n")
    return exit_code_for(exc)


def _run_once(config: Config, args, labels, *, client, stdout, stderr) -> int:
    """Single snapshot -> render -> exit. Honors --format. On success in
    json mode, stdout carries ONLY the JSON document.
    """
    fmt = args.format
    owns_client = client is None
    try:
        c = client if client is not None else Client(config)
        try:
            now = datetime.now(timezone.utc)
            snap = _do_one_fetch(
                c,
                config.host,
                now,
                filter_repo=args.repo,
                filter_label=labels,
                config=config,
            )
        finally:
            if owns_client:
                c.close()
    except FjQueueError as e:
        return _emit_error(
            e, host=config.host, fmt=fmt, token=config.token,
            stdout=stdout, stderr=stderr,
        )

    if fmt == "json":
        stdout.write(render_json(snap) + "\n")
    elif fmt == "plain":
        stdout.write(render_plain(snap) + "\n")
    else:
        from rich.console import Console
        Console(file=stdout).print(render_rich(snap))
    return EXIT_OK


def main(argv=None, *, client=None, stdout=None, stderr=None, is_tty=None) -> int:
    """CLI entry point. Returns the process exit code.

    Injectable seams for tests: `client` (a pre-built Client or
    FakeClient), `stdout`/`stderr` (streams), and `is_tty` (override
    the terminal probe used for default-mode resolution).
    """
    import sys

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits: 0 for --help/--version, 2 for usage errors.
        code = e.code
        return code if isinstance(code, int) else EXIT_USAGE

    # --schema short-circuits before any auth/network.
    if args.schema:
        out.write(render_schema() + "\n")
        return EXIT_OK

    if is_tty is None:
        is_tty = out.isatty() if hasattr(out, "isatty") else False
    mode = _resolve_mode(args, is_tty=is_tty, stderr=err)

    # Token resolution: a missing token is a usage error (exit 2).
    try:
        token = resolve_token(args.token)
    except ConfigError as e:
        return _emit_error(
            e, host=args.host, fmt=args.format, token=None,
            stdout=out, stderr=err,
        )

    config = Config(
        host=args.host,
        token=token,
        timeout=args.timeout,
        metrics_enabled=args.metrics,
        metrics_url=resolve_metrics_url(args.metrics_url),
        metrics_namespace=args.metrics_namespace,
        metrics_cluster=args.metrics_cluster,
    )
    labels = tuple(args.label or ())

    if mode == "watch":
        try:
            from rich.console import Console
            console = Console(file=out)
            return run_watch(
                config,
                interval=args.interval,
                format_mode=args.format,
                filter_repo=args.repo,
                filter_label=labels,
                client=client,
                console=console,
            )
        except FjQueueError as e:
            return _emit_error(
                e, host=config.host, fmt=args.format, token=config.token,
                stdout=out, stderr=err,
            )

    return _run_once(config, args, labels, client=client, stdout=out, stderr=err)


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
