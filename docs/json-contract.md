# JSON Contract

`--format json` produces a stable, byte-deterministic document that is the
stability contract for agent consumers. The schema is committed at
`schema/fj-queue.v1.json`.

## Fetching the schema

```bash
uv run fj_queue.py --schema | jq .
uv run fj_queue.py --schema > fj-queue.v1.json   # cache locally
```

`--schema` prints the schema to stdout and exits 0.

## Top-level shape

A success snapshot:

```jsonc
{
  "schema_version": 1,
  "tool_version": "0.0.1",                 // fj-queue release (see below)
  "as_of": "2026-05-26T14:03:11Z",        // RFC3339 UTC fetch timestamp
  "host": "git.example.com",
  "filter": { "repo": null, "label": null },  // echoes --repo / --label
  "schedulable_labels": ["grunt", "docker"],  // union of ONLINE runners' labels
  "runners": [ /* Runner objects, sorted by name */ ],
  "totals": { "running": 6, "waiting": 31, "total": 37 },  // instance-wide
  "per_repo": [ /* RepoBreakdown objects, sorted by repo slug */ ],
  "queue":   [ /* Job objects (waiting only), sorted by job_id ascending */ ],
  "warnings":[ /* Warning objects, sorted by job_id */ ],
  "runner_pods": [ /* PodResource objects, sorted by pod name */ ],
  "metrics": { "source": "prometheus", "error": null, "rate_window": "5m" },
  "ncps": { /* NcpsStatus object, or null when disabled or fetch failed */ },
  "ncps_error": null           // "disabled" when NCPS is OFF (--no-ncps / config [ncps] enabled=false); null on success or fetch failure
}
```

An error snapshot (also emitted on stdout so an agent gets parseable JSON
even on failure):

```jsonc
{
  "schema_version": 1,
  "error": { "code": "auth", "message": "...", "host": "git.example.com" }
}
```

## Object shapes

`runner_pods` entry:

```jsonc
{
  "pod": "ci-runner-aaaa1111ff-pod1",
  "node": "node-a-1",
  "cpu_cores": 0.00109,             // 5m-rate CPU usage in cores, combined containers
  "memory_bytes": 763322368,        // working-set memory in bytes, combined containers
  "cpu_limit_cores": null,          // configured CPU limit (null when unset)
  "memory_limit_bytes": 18674094196 // configured memory limit in bytes (null when unset)
}
```

`ncps` object (when NCPS is enabled and the fetch succeeded):

```jsonc
{
  "active": true,             // true when serving requests (req/s > 0 or in-flight > 0)
  "requests_per_sec": 8.5,    // requests/sec over the rate window
  "inflight": 1,              // requests in flight right now
  "upstream_per_sec": 0.3,    // cache misses/sec (NCPS to upstream cache)
  "bytes_per_sec": 4000000.0  // bytes/sec served (pod network egress)
}
```

When metrics or NCPS is disabled, or when the Prometheus fetch fails,
`runner_pods` is `[]` and `metrics.error` holds the reason string. `ncps` is
`null`. The keys are always present for contract stability.

## Ordering guarantees

The JSON is serialized with `sort_keys=True`. Within arrays:

- `queue` is sorted by `job_id` ascending (FIFO approximation; see [Caveats](caveats.md)).
- `per_repo` is sorted by repo slug.
- `runners` is sorted by name.
- `warnings` is sorted by `job_id`.
- `runner_pods` is sorted by pod name.

`position` on each `queue` entry is 1-based over waiting jobs only.

## Open additive-only schema policy

Inner objects (`Runner`, `Job`, `RepoBreakdown`, `totals`, `filter`, `error`,
`Warning`, `PodResource`, `metrics`) are **open**: they do not set
`additionalProperties: false`, so new optional fields can land in v1.x
**without bumping `schema_version`**. Only the two top-level envelope
branches (`SuccessEnvelope`, `ErrorEnvelope`) are closed, to disambiguate
success from error.

`schema_version` bumps only when an existing key is removed or repurposed.
Agents should treat unknown inner-object fields as forward-compatible
additions.

## tool_version vs schema_version

`tool_version` is the fj-queue release string (e.g. `"0.0.1"`). It is a
**required** top-level field in every success snapshot, always present.

`schema_version` (currently `1`) describes the JSON wire-format contract: it
changes only when the envelope shape breaks backwards compatibility.

The two fields are independent: the tool version advances with every release;
the schema version stays `1` as long as the contract is additive. Agents that
need to log or branch on which version of fj-queue produced a document should
read `tool_version`; agents validating document structure should check
`schema_version`.
