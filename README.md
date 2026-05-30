# fj-queue

A read-only Forgejo Actions runner and CI queue dashboard. Polls the admin
Actions API on `git.wxs.ro` (or any Forgejo host) and renders runner
inventory, queue totals, per-repo backlog, FIFO-approximate queue order,
unschedulable-job warnings, and per-pod runner CPU/memory usage (from
Prometheus).

Designed for two audiences:

- **Humans**: a live, in-place refreshing terminal UI (Rich) so an operator
  can see at a glance whether CI is saturated.
- **Agents**: a stable, byte-deterministic JSON document
  (`--format json`) backed by a committed JSON Schema, so AI agents and
  scripts can branch on global saturation, a repo's queue position, and
  structured `blocked_reason` / `warnings` fields.

Tracked under PRD [#61](https://git.wxs.ro/wxs/ai-tasks/issues/61).

## Setup

Requirements:

- Python 3.11 or newer.
- [`uv`](https://docs.astral.sh/uv/) installed on the host (including any
  agent host that shells out to `fj_queue.py`). Runtime libraries (`rich`,
  `httpx`) are pinned via a PEP-723 inline header, so `uv run` resolves
  and caches them on first invocation; no manual venv setup.
- An **admin-scoped** Forgejo API token. The dashboard talks to
  `/api/v1/admin/actions/runners[,/jobs]`, which require `is_admin: true`.
  A non-admin token gets 403 and the tool exits 3.

No installation step. Either run the script directly:

```bash
uv run fj_queue.py
```

or via the bundled `fj-queue` symlink:

```bash
./fj-queue
```

## Authentication

Token resolution order (first match wins):

1. `--token <value>` on the command line.
2. `$FORGEJO_TOKEN` in the environment.
3. (Optional) `tea` config at `~/.config/tea/config.yml`. **Currently
   dropped** to avoid pulling in `pyyaml` as a runtime dependency; see
   PRD #61 §Authentication. Use `$FORGEJO_TOKEN` or `--token` instead.

Tokens are scrubbed from all output paths (JSON error envelope, stderr
diagnostics, Rich watch-mode error panel) so a leaked token in
`str(exc)` cannot escape to a log or pipe.

The default `--host` is `git.wxs.ro`. Point at another Forgejo instance
with `--host git.example.com`.

## Flags

| Flag | Value | Description |
|---|---|---|
| `--mode {watch,once}` | enum | Run mode. `watch` keeps refreshing on `--interval`; `once` renders one snapshot and exits. Default: `watch` at a TTY, `once` when stdout is piped. `--format json` always forces `once`. |
| `--watch` | (alias) | Equivalent to `--mode watch`. Mutually exclusive with `--once` and `--mode`. |
| `--once` | (alias) | Equivalent to `--mode once`. Mutually exclusive with `--watch` and `--mode`. |
| `--format {rich,plain,json}` | enum | Output format. `rich` (default): styled tables/panels via `rich.live.Live(screen=True)`. `plain`: no-color, no box-drawing, `NO_COLOR`-aware, pipe-friendly. `json`: single stable JSON document (forces `--once`). |
| `--interval SECONDS` | float, default 2.0 | Watch-mode poll interval in seconds. |
| `--host HOST` | string, default `git.wxs.ro` | Forgejo host. A bare hostname is auto-prefixed with `https://`; values that already start with `http://` or `https://` are accepted as-is. A malformed value (e.g. CRLF or other characters httpx rejects as `InvalidURL`) is rejected with exit 2 and a JSON error envelope on stdout. |
| `--token TOKEN` | string | API token. Overrides `$FORGEJO_TOKEN`. Needs admin scope. |
| `--timeout SECONDS` | float, default 10.0 | Per-request HTTP timeout. |
| `--label LABEL` | repeatable | Scope `queue` to jobs whose `runs_on` includes at least this label (subset filter, evaluated client-side). Repeat for multiple labels. See [Filter semantics](#filter-semantics) for what filtering does and does not affect. |
| `--repo OWNER/REPO` | string | Scope `queue` to a single repo slug. Same filter semantics as `--label`. |
| `--no-metrics` | flag | Disable per-pod runner CPU/memory metrics. Metrics are on by default and degrade gracefully (a Prometheus failure shows "unavailable" and never crashes the dashboard). See [Runner resources](#runner-resources-per-pod). |
| `--metrics-url URL` | string, default `https://prometheus.wxs.ro` | Prometheus base URL for the runner-metrics queries. Also settable via `$FJ_QUEUE_METRICS_URL` (the `--metrics-url` flag wins). No auth is sent to this endpoint. |
| `--metrics-namespace NS` | string, default `forgejo-runner` | Kubernetes namespace of the runner pods. |
| `--metrics-cluster {auto,green,blue}` | enum, default `auto` | Blue/green filter. Both clusters report `cluster="k8s-cc"`, so the live one is told apart by node prefix (`k8s-green-*` vs `k8s-blue-*`). `auto` applies no filter; `green` / `blue` keep only pods on that color's nodes. |
| `--schema` | flag | Print the JSON Schema (`schema/fj-queue.v1.json`) to stdout and exit 0. |
| `--version` | flag | Print `fj-queue <version>` and exit. |
| `--help` | flag | argparse-generated help. |

## JSON contract and schema

The `--format json` document is a stability contract for agent
consumers. The schema is committed at:

```
scripts/fj-queue/schema/fj-queue.v1.json
```

Fetch the schema at runtime with `--schema`:

```bash
uv run fj_queue.py --schema | jq .
```

### Top-level shape

A success snapshot:

```jsonc
{
  "schema_version": 1,
  "as_of": "2026-05-26T14:03:11Z",       // RFC3339 UTC fetch timestamp
  "host": "git.wxs.ro",
  "filter": { "repo": null, "label": null },   // echoes --repo / --label
  "schedulable_labels": ["grunt", "docker"],   // union of ONLINE runners' labels
  "runners": [ /* Runner objects, sorted by name */ ],
  "totals": { "running": 6, "waiting": 31, "total": 37 },  // instance-wide
  "per_repo": [ /* RepoBreakdown objects, sorted by repo slug */ ],
  "queue":   [ /* Job objects (waiting only), sorted by job_id ascending */ ],
  "warnings":[ /* Warning objects, sorted by job_id */ ],
  "runner_pods": [ /* PodResource objects, sorted by pod name (raw numbers) */ ],
  "metrics": { "source": "prometheus", "error": null, "rate_window": "5m" },
  "ncps": { /* NcpsStatus object, or null when disabled/failed */ }
}
```

The `ncps` object reports NCPS (the nix cache proxy) cache activity, or
`null` when metrics are disabled or the NCPS fetch failed:

```jsonc
{
  "active": true,            // serving requests now (req/s > 0 or in-flight > 0)
  "requests_per_sec": 8.5,   // requests/sec over the rate window
  "inflight": 1,             // requests in flight right now
  "upstream_per_sec": 0.3,   // cache misses/sec (NCPS -> cache.nixos.org)
  "bytes_per_sec": 4000000.0 // bytes/sec served (NCPS pod network egress)
}
```

Each `runner_pods` entry carries raw numbers (agents parse these; the
human renderers format CPU to 3 decimal cores and memory to MiB):

```jsonc
{
  "pod": "forgejo-runner-54746685ff-qf4k7",
  "node": "k8s-green-wn2",
  "cpu_cores": 0.00109,           // 5m-rate CPU usage in cores, combined containers
  "memory_bytes": 763322368,      // working-set memory in bytes, combined containers
  "cpu_limit_cores": null,        // configured CPU limit (null when unset; currently unset)
  "memory_limit_bytes": 18674094196  // configured memory limit in bytes (null when unset)
}
```

`runner_pods` and `metrics` are always present for contract stability.
On a Prometheus failure, `runner_pods` is `[]` and `metrics.error` holds
the reason string. See [Runner resources](#runner-resources-per-pod).

An error snapshot (also emitted on stdout, so an agent gets parseable
JSON even on failure):

```jsonc
{
  "schema_version": 1,
  "error": { "code": "auth", "message": "...", "host": "git.wxs.ro" }
}
```

### Ordering guarantees

The JSON is serialized with `sort_keys=True`. Within arrays:

- `queue` is sorted by `job_id` ascending (FIFO approximation; see caveats).
- `per_repo` is sorted by repo slug.
- `runners` is sorted by name.
- `warnings` is sorted by `job_id`.
- `runner_pods` is sorted by pod name.

`position` on each `queue` entry is 1-based over waiting jobs only.

### Open additive-only schema policy

Inner objects (`Runner`, `Job`, `RepoBreakdown`, `totals`, `filter`,
`error`, `Warning`, `PodResource`, `metrics`) are **open**: they do NOT set
`additionalProperties: false`, so new optional fields can land in v1.x
**without bumping `schema_version`**. Only the two top-level envelope
branches (`SuccessEnvelope`, `ErrorEnvelope`) are closed, and only to
disambiguate success vs error.

`schema_version` bumps only when an existing key is **removed or
repurposed**. Agents should treat unknown inner-object fields as
forward-compatible additions, not breakage.

## Runner resources (per pod)

The Forgejo admin API exposes a runner's id, name, status, version,
labels, and ephemeral flag, but no CPU or memory. The source of truth
for resource usage is **Prometheus** (`https://prometheus.wxs.ro`, no
auth, fed by cAdvisor and kube-state-metrics).

There is no per-runner-row join key: every live runner shares the API
name `k8s-runner`, the API never exposes the pod name, and Prometheus
never sees the runner UUID. So this is a **separate per-pod section**,
not extra columns on the runner inventory rows.

Five PromQL queries are joined by pod:

1. CPU cores per pod (combines the `runner` agent and `dind` containers):
   `sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="forgejo-runner", container!=""}[5m]))`
2. Working-set memory bytes per pod:
   `sum by (pod) (container_memory_working_set_bytes{namespace="forgejo-runner", container!=""})`
3. pod to node map (for the blue/green filter):
   `kube_pod_info{namespace="forgejo-runner"}`
4. Memory limit bytes per pod:
   `sum by (pod) (kube_pod_container_resource_limits{namespace="forgejo-runner", resource="memory"})`
5. CPU limit cores per pod:
   `sum by (pod) (kube_pod_container_resource_limits{namespace="forgejo-runner", resource="cpu"})`

Behavior:

- **Per pod only**, with the two containers combined per pod. There is
  no fleet-total line.
- **Usage and limit**: each cell shows `usage / limit`. Memory limits
  are set (about 17.4 GiB per pod) and CPU limits are not, so the limit
  fields are nullable. In the JSON, `cpu_limit_cores` and
  `memory_limit_bytes` are raw numbers (or null when unset); the human
  renderers show a dash for an unset limit denominator (for example
  `cpu=0.001 / -`).
- **Always on, graceful degradation**: metrics are fetched every tick
  from a Prometheus client that is fully isolated from the Forgejo
  client (separate connection, no `Authorization` header). Any failure
  (timeout, HTTP error, malformed body, bad URL) shows an "unavailable"
  line and never crashes the dashboard. Disable with `--no-metrics`,
  which sets `metrics.error` to `"disabled"` so a JSON consumer can tell
  a disabled run apart from a successful fetch that found zero pods
  (which is `runner_pods: []` with `metrics.error: null`).
- **Human formatting**: CPU is shown to 3 decimal cores (for example
  `0.015`); memory uses MiB below 1024 MiB and GiB at or above it, to 1
  decimal (for example `728 MiB / 17.4 GiB`). The JSON keeps the raw
  `cpu_cores`, `memory_bytes`, `cpu_limit_cores`, and `memory_limit_bytes`
  numbers.
- **Blue/green**: both clusters report `cluster="k8s-cc"`, so the live
  one is identified by node prefix. `--metrics-cluster green|blue` keeps
  only pods whose node starts with `k8s-<color>-`; the default `auto`
  applies no filter.

## NCPS cache status

fj-queue also reports whether NCPS (the nix cache proxy at
nix-cache.wxs.ro) is actively serving nix packages or sitting idle, from
the same Prometheus source (job `ncps`). It is one line, not a table:

- plain: `NCPS: active (8.5 req/s, 4 MiB/s, 0.3 miss/s)` or `NCPS: idle`.
- rich: same text, green when active and dim when idle.

NCPS is **active** iff it is serving requests right now (requests/sec
above zero) or has any in-flight requests; otherwise **idle**. The four
queries (requests/sec, in-flight, upstream-miss/sec, bytes/sec) use a 2m
rate window. Throughput is measured as the NCPS pod's network egress
(cAdvisor `container_network_transmit_bytes_total`), because NCPS does
not record sizes for streamed nar bodies, so its `response_size_bytes`
sum is structurally zero. This shares the metrics plumbing: `--no-metrics` turns it
off (`NCPS: disabled (--no-metrics)`, JSON `ncps: null`) and a Prometheus
failure degrades to `NCPS: unavailable (<reason>)` (JSON `ncps: null`).
The JSON keeps raw numbers; the human renderers format req/s and miss/s
to 1 decimal and bytes/s via the MiB/GiB helper.

## Exit codes

For agent consumers branching on outcome:

| Code | Meaning |
|---|---|
| 0 | Ok, including an empty queue. |
| 2 | Usage error: bad flags, missing token, malformed `--host`. |
| 3 | Auth: 401 (bad token) or 403 (token lacks admin scope). |
| 4 | Connection / timeout / 5xx / 429. |
| 5 | Schema drift: the upstream response did not match the expected shape. Emitted as a typed JSON error, not a stack trace. |
| 130 | Interrupted by Ctrl-C in `--mode watch` (UNIX `128 + SIGINT`, matches htop/top/less convention). |

On 3/4/5, `--format json` still emits a parseable error envelope on
stdout; a human-readable diagnostic always goes to stderr.

## Read-only by design

`fj-queue` exercises **no** mutation endpoints. Verified on 2026-05-26
against this instance's swagger, Codeberg's latest-Forgejo swagger, and
Forgejo docs: the Forgejo REST API exposes Actions runs/tasks as
GET-only. Cancellation, rerun, and delete exist only behind web-UI
POST routes (cookie + CSRF). The tool deliberately does not implement
those, so it is safe to point at a busy production instance.

See PRD #61 §"Explicitly out of scope" for the full list.

## Caveats

Three caveats are load-bearing for correct interpretation of the
output.

### FIFO approximation

The `queue` array is sorted by job `id` ascending. The real Forgejo
dispatcher orders available jobs by `(updated, id)` ASC and considers
only jobs with `status = waiting AND task_id = 0`. So the displayed
queue order is a **documented approximation** of "roughly who's next,"
not the literal dispatch order. Good for human triage and for an agent
asking "is my repo near the front?"; not a contract on which exact job
the next freed runner will pick up.

### `blocked` jobs are invisible

The `/api/v1/admin/actions/runners/jobs` endpoint returns only jobs in
status `waiting` or `running`. Jobs in `blocked` state (`needs`
dependencies unmet, no task assigned) are **not returned at all**.
`fj-queue` documents this gap rather than papering over it: a job that
disappears from the dashboard may have moved to `blocked`, not
finished. Use the per-repo web UI to see truly blocked jobs.

### Filter semantics

`--repo` and `--label` scope **only** `Snapshot.queue`. The following
fields are **always instance-wide**, regardless of filtering:

- `totals`
- `per_repo`
- `runners`
- `warnings`
- `schedulable_labels`

Each surviving entry in a filtered `queue` keeps its **global**
`position` (gaps in positions are intentional, not a bug).

This is so the agent journey, "read global saturation from `totals`,
then find my repo's backlog in `per_repo` / `queue`," works correctly
under any filter. If the filter scoped totals as well, an agent could
not tell whether the instance is saturated or just its own repo is.

`--label` is also distinct from the server-side `?labels=` filter on
the Forgejo API: the server-side filter means "jobs whose `runs_on`
includes at least these labels," which is **not** the same as "jobs a
runner with these labels can run." `fj-queue` applies `--label`
client-side, after aggregation, with the documented semantics above.

## Usage examples

Human, live dashboard (the default):

```bash
uv run fj_queue.py
```

Single snapshot in plain text, suitable for `grep` / pipes:

```bash
uv run fj_queue.py --once --format plain
```

Agent, JSON snapshot scoped to one repo, piped to `jq`:

```bash
uv run fj_queue.py --format json --repo containers/my-app \
  | jq '.queue[0].position'
```

Agent, watch global saturation and act when running drops below a
threshold (still uses `--once` because `--format json` forces it):

```bash
while sleep 30; do
  running=$(uv run fj_queue.py --format json | jq '.totals.running')
  [ "$running" -lt 4 ] && break
done
```

Fetch the JSON Schema for offline validation:

```bash
uv run fj_queue.py --schema > fj-queue.v1.json
```

## See also

- PRD: [`prds/61-fj-queue.md`](https://git.wxs.ro/wxs/ai-tasks/src/branch/main/prds/61-fj-queue.md) in `wxs/ai-tasks` (full design rationale, scheduling-semantics source verification, milestones).
- Schema file: `scripts/fj-queue/schema/fj-queue.v1.json`.
- Source: `scripts/fj-queue/fj_queue.py`.
