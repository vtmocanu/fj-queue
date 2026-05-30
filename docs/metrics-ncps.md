# Metrics and NCPS

Both features are **opt-in** and **off by default**. They share the same
Prometheus source and the same `--metrics-url` / `$FJ_QUEUE_METRICS_URL` /
`[metrics] url` configuration key. A Prometheus URL is required whenever
either feature is enabled; enabling without one exits 2.

## Per-pod runner CPU/memory metrics

Enabled with `--metrics` or `[metrics] enabled = true` in the config file.

Because the Forgejo admin API does not expose pod names or resource usage,
fj-queue queries Prometheus directly. Five PromQL queries are joined by pod:

1. CPU cores (5m rate, both containers combined per pod):
   `sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="<ns>", container!=""}[5m]))`
2. Working-set memory bytes:
   `sum by (pod) (container_memory_working_set_bytes{namespace="<ns>", container!=""})`
3. Pod-to-node map (for `--node-prefix` filtering):
   `kube_pod_info{namespace="<ns>"}`
4. Memory limit bytes:
   `sum by (pod) (kube_pod_container_resource_limits{namespace="<ns>", resource="memory"})`
5. CPU limit cores:
   `sum by (pod) (kube_pod_container_resource_limits{namespace="<ns>", resource="cpu"})`

Replace `<ns>` with the configured namespace (default: `ci-runners`).

**Node prefix filter:** `--node-prefix PREFIX` (or `[metrics] node_prefix`)
keeps only pods whose Kubernetes node name starts with `PREFIX`. Leave it
empty (the default) to include all pods. This is a free-form string; it
replaces the old `--metrics-cluster` enum.

**Human formatting:** CPU is shown to 3 decimal cores (for example `0.015`);
memory uses MiB below 1024 MiB and GiB at or above it, to 1 decimal (for
example `728 MiB / 17.4 GiB`). The JSON keeps raw numbers.

**Graceful degradation:** any Prometheus failure (timeout, HTTP error,
malformed body) shows an "unavailable" line in the UI and never crashes the
dashboard. In the JSON, `runner_pods` is `[]` and `metrics.error` holds the
reason string. A clean disabled run sets `metrics.error` to `"disabled"`, so
a JSON consumer can distinguish disabled from a successful fetch that found
zero pods (`runner_pods: []`, `metrics.error: null`).

There is no per-runner-row join: every live runner shares the same API name,
and the API does not expose pod names. The pod section is therefore separate
from the runner inventory rows.

## NCPS cache-status display

[NCPS](https://github.com/kalbasit/ncps) (Nix Cache Proxy Server) is a local Nix binary cache that pulls store paths from upstream caches and serves them on your network to speed up Nix builds; you point Nix-based CI runners at it so jobs reuse cached artifacts instead of re-downloading them. This section reports whether that cache is actively serving packages or sitting idle.

Enabled with `--ncps` or `[ncps] enabled = true` in the config file.
Independent of `[metrics] enabled`.

Reports whether a nix cache proxy service is actively serving nix packages or
sitting idle, from the same Prometheus source (job `ncps`).

Display:

- plain: `NCPS: active (8.5 req/s, 4 MiB/s, 0.3 miss/s)` or `NCPS: idle`
- rich: same text, green when active and dim when idle

NCPS is **active** when requests/sec is above zero or any request is in
flight; otherwise **idle**. Throughput is measured as network egress from the
NCPS pod (cAdvisor `container_network_transmit_bytes_total`), because NCPS
does not record sizes for streamed nar bodies.

The four queries use a 2m rate window. A Prometheus failure degrades to
`NCPS: unavailable (<reason>)` in the UI; in the JSON, `ncps` is `null`.

## Enabling in config

```toml
[metrics]
enabled = true
url = "https://prometheus.example.com"
namespace = "ci-runners"
node_prefix = "k8s-node-"   # optional; leave empty to include all pods

[ncps]
enabled = true
```

Both sections use the same `url` (from `[metrics] url`).
