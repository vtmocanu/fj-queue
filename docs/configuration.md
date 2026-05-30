# Configuration

## Precedence

For each setting, the highest-precedence source wins:

1. CLI flag
2. Environment variable
3. Config file
4. Built-in default (where one exists)

## Settings reference

| Setting | Flag | Env var | Config key | Default |
|---|---|---|---|---|
| Forgejo host | `--host HOST` | `$FORGEJO_HOST` | `host` | none (required) |
| API token | `--token TOKEN` | `$FORGEJO_TOKEN` | (never in config) | none (required) |
| Config file path | `--config PATH` | - | - | (auto-discovered) |
| Output format | `--format {rich,plain,json}` | - | - | `rich` |
| Run mode | `--mode {watch,once}` | - | - | `watch` at TTY, `once` when piped |
| Poll interval | `--interval SECONDS` | - | - | `2.0` |
| Request timeout | `--timeout SECONDS` | - | - | `10.0` |
| Repo filter | `--repo OWNER/REPO` | - | - | (none) |
| Label filter | `--label LABEL` | - | - | (none) |
| Metrics enabled | `--metrics` / `--no-metrics` | - | `[metrics] enabled` | `false` |
| Metrics URL | `--metrics-url URL` | `$FJ_QUEUE_METRICS_URL` | `[metrics] url` | none (required when metrics or ncps on) |
| Metrics namespace | `--metrics-namespace NS` | - | `[metrics] namespace` | `ci-runners` |
| Node prefix | `--node-prefix PREFIX` | - | `[metrics] node_prefix` | (empty, no filter) |
| NCPS enabled | `--ncps` / `--no-ncps` | - | `[ncps] enabled` | `false` |

## Config file format

The config file uses [TOML](https://toml.io/) and is parsed with the stdlib
`tomllib` (no extra dependency). The **API token is never read from or written
to the config file**; use `--token` or `$FORGEJO_TOKEN`.

```toml
host = "git.example.com"

[metrics]
enabled = true
url = "https://prometheus.example.com"
namespace = "ci-runners"
node_prefix = "k8s-node-"

[ncps]
enabled = true
```

See [`config.toml.example`](../config.toml.example) at the repo root for a
fully annotated template.

## Config file discovery

If `--config PATH` is not given, fj-queue looks for a config file in this
order (first found wins):

1. `./fj-queue.toml` (current directory)
2. `$XDG_CONFIG_HOME/fj-queue/config.toml`
3. `~/.config/fj-queue/config.toml` (fallback when `$XDG_CONFIG_HOME` is
   unset)

If none is found, only CLI flags and environment variables apply.

## Required settings

**Host** (`--host` / `$FORGEJO_HOST` / `host` in config) is required. If it
is not provided by any source, fj-queue exits 2 with guidance pointing at the
three resolution paths.

**Token** (`--token` / `$FORGEJO_TOKEN`) is required for all API calls. It
must have admin scope.

## Opt-in features: metrics and NCPS

Per-pod runner CPU/memory metrics and NCPS cache-status display are both **off
by default**. Enable them independently:

```bash
# Enable metrics for one run
uv run fj_queue.py --host git.example.com --metrics --metrics-url https://prometheus.example.com

# Enable NCPS for one run
uv run fj_queue.py --host git.example.com --ncps --metrics-url https://prometheus.example.com

# Enable both for one run
uv run fj_queue.py --host git.example.com --metrics --ncps --metrics-url https://prometheus.example.com
```

Or set them persistently in the config file (see above).

**A Prometheus URL is required whenever metrics or NCPS is enabled.** Enabling
either feature without a URL exits 2. There is no built-in default URL.

See [Metrics and NCPS](metrics-ncps.md) for what these features show and how
they work.

## `--no-metrics` and `--no-ncps`

These flags explicitly disable the features, overriding any config-file
setting. They are redundant with the default-off behavior but useful in
scripts that need to guarantee a feature is off regardless of the local
config file.
