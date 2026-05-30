# Usage

## Output formats

| Format | Description |
|---|---|
| `rich` (default) | Styled tables and panels via Rich, rendered in-place on a live terminal. |
| `plain` | No color, no box-drawing characters. Respects `NO_COLOR`. Pipe-friendly. |
| `json` | Single stable JSON document (always forces `--once`). See [JSON contract](json-contract.md). |

```bash
# Default (rich, live refresh)
uv run fj_queue.py --host git.example.com

# Plain text, suitable for grep / pipes
uv run fj_queue.py --host git.example.com --format plain

# JSON snapshot
uv run fj_queue.py --host git.example.com --format json
```

## Run modes

| Mode | Description |
|---|---|
| `watch` | Live refresh on `--interval` (default: 2.0 s). Default at a TTY. |
| `once` | Single snapshot, then exit. Default when stdout is piped. Always used with `--format json`. |

```bash
# Force watch mode
uv run fj_queue.py --host git.example.com --mode watch
# Aliases
uv run fj_queue.py --host git.example.com --watch

# Force once mode
uv run fj_queue.py --host git.example.com --mode once
# Aliases
uv run fj_queue.py --host git.example.com --once
```

## Filtering

`--repo` and `--label` narrow the `queue` section to jobs matching the
filter. All other sections (`totals`, `per_repo`, `runners`, `warnings`,
`schedulable_labels`) remain instance-wide. See
[Caveats: filter semantics](caveats.md#filter-semantics) for details.

```bash
# Scope queue to one repo
uv run fj_queue.py --host git.example.com --repo owner-a/repo-a

# Scope queue to jobs whose runs_on includes at least this label
uv run fj_queue.py --host git.example.com --label docker

# Combine (both must match)
uv run fj_queue.py --host git.example.com --repo owner-a/repo-a --label docker
```

`--label` is repeatable. All supplied labels must be in the job's `runs_on`
(subset filter, client-side).

## Common examples

Human, live dashboard (the default at a TTY):

```bash
uv run fj_queue.py --host git.example.com
```

Single snapshot in plain text:

```bash
uv run fj_queue.py --host git.example.com --once --format plain
```

Agent: JSON snapshot scoped to one repo, piped to `jq`:

```bash
uv run fj_queue.py --host git.example.com --format json --repo owner-a/repo-a \
  | jq '.queue[0].position'
```

Agent: poll until global running count drops below a threshold:

```bash
while sleep 30; do
  running=$(uv run fj_queue.py --host git.example.com --format json \
            | jq '.totals.running')
  [ "$running" -lt 4 ] && break
done
```

Fetch and cache the JSON Schema:

```bash
uv run fj_queue.py --schema > fj-queue.v1.json
```

## Flags summary

| Flag | Value | Description |
|---|---|---|
| `--host HOST` | string | Forgejo host. Bare hostname is auto-prefixed with `https://`. Required (no built-in default). |
| `--token TOKEN` | string | API token. Overrides `$FORGEJO_TOKEN`. Needs admin scope. |
| `--config PATH` | path | Path to TOML config file (overrides auto-discovery). |
| `--mode {watch,once}` | enum | Run mode. Default: `watch` at a TTY, `once` when piped. |
| `--watch` | alias | Equivalent to `--mode watch`. |
| `--once` | alias | Equivalent to `--mode once`. |
| `--format {rich,plain,json}` | enum | Output format. `json` forces `--once`. Default: `rich`. |
| `--interval SECONDS` | float | Watch-mode poll interval. Default: `2.0`. |
| `--timeout SECONDS` | float | Per-request HTTP timeout. Default: `10.0`. |
| `--repo OWNER/REPO` | string | Scope `queue` to a single repo slug. |
| `--label LABEL` | repeatable | Scope `queue` to jobs whose `runs_on` includes this label. |
| `--metrics` / `--no-metrics` | flag | Enable or explicitly disable per-pod metrics. Off by default. |
| `--metrics-url URL` | string | Prometheus base URL. Required when metrics or NCPS is on. |
| `--metrics-namespace NS` | string | Kubernetes namespace of the runner pods. Default: `ci-runners`. |
| `--node-prefix PREFIX` | string | Filter runner pods by node name prefix. Empty means no filter. |
| `--ncps` / `--no-ncps` | flag | Enable or explicitly disable NCPS cache-status. Off by default. |
| `--schema` | flag | Print the JSON Schema to stdout and exit 0. |
| `--version` | flag | Print `fj-queue <version>` and exit. |
| `--help` | flag | Show help and exit. |

## Exit codes

For agent consumers branching on outcome:

| Code | Meaning |
|---|---|
| 0 | Ok, including an empty queue. |
| 2 | Usage error: bad flags, missing host or token, missing metrics URL when metrics/NCPS on, malformed `--host`. |
| 3 | Auth: 401 (bad token) or 403 (token lacks admin scope). |
| 4 | Connection error, timeout, 5xx, or 429. |
| 5 | Schema drift: the upstream response did not match the expected shape. Emitted as a typed JSON error, not a stack trace. |
| 130 | Interrupted by Ctrl-C in `--mode watch` (UNIX `128 + SIGINT`). |

On exit codes 3, 4, and 5, `--format json` still emits a parseable error
envelope on stdout; a human-readable diagnostic always goes to stderr.
