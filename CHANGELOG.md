# Changelog

All notable changes to fj-queue are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] - 2026-05-31

### Added

- **`tool_version` in JSON output.** `--format json` now includes a top-level
  `"tool_version"` string field (e.g. `"0.1.0"`) in every success snapshot.
  It is required and always present. This field carries the fj-queue release
  that produced the document, and is distinct from `schema_version` (the
  wire-format contract version, which stays `1`). Agents that need to log or
  branch on which fj-queue version produced a snapshot should read
  `tool_version`; agents validating document structure should check
  `schema_version`.
- **Version in renderer headers.** The plain-text header now reads
  `fj-queue v<version>  as_of=...  host=...`. The Rich renderer header shows
  `fj-queue` followed by a dim `v<version>` then `@ <host>`.

### Changed

- **Release tooling.** The Homebrew formula render/publish now consumes the
  reusable `homebrew-tap.yml` from `vtmocanu/task`; this repo keeps only the
  formula body in `Formula.rb.tmpl` (placeholders `@@URL@@` / `@@SHA256@@`).
  No change to how `brew install fj-queue` works.

---

## [2.0.0] - 2026-05-30

### Breaking

- **Host is now required.** Running without `--host` / `$FORGEJO_HOST` / config
  `host` key exits with code 2 and an actionable error message. Previously a
  private default was baked in.
- **`--metrics-cluster` removed.** Use `--node-prefix` instead (free-form string;
  e.g. `--node-prefix k8s-node-` replaces `--metrics-cluster green`).
- **Metrics and NCPS default to OFF.** Opt in explicitly via `--metrics` /
  `--ncps` or config `[metrics] enabled = true` / `[ncps] enabled = true`.
  Previously metrics were ON by default, which would hit a private Prometheus
  endpoint on every invocation.
- **Prometheus URL required when metrics or NCPS is enabled.** Omitting
  `--metrics-url` / `$FJ_QUEUE_METRICS_URL` / config `[metrics] url` while
  metrics or NCPS is ON exits with code 2. No private URL ships as a default.

### Added

- **TOML config file** with layered precedence: CLI flag > env var > config file >
  built-in default. Auto-discovered at `./fj-queue.toml` then
  `$XDG_CONFIG_HOME/fj-queue/config.toml` then `~/.config/fj-queue/config.toml`.
- **`--config PATH`** flag to supply an explicit config file path.
- **`--metrics` / `--no-metrics`** flag pair (replaces always-on default).
- **`--ncps` / `--no-ncps`** flag pair for independent NCPS control.
- **`--node-prefix`** free-form string flag (replaces `--metrics-cluster`).
- **`ncps_error` JSON field** at the top level of every success snapshot.
  Value is `"disabled"` when NCPS is OFF (`--no-ncps` / config); `null` on
  success or when the fetch fails (see `ncps` field for result).
- **`config.toml.example`** committed to the repo as a copy-paste template.
- **Environment-leak guard test** (`tests/test_no_env_leaks.py`): fails CI if
  internal host names, pod names, org slugs, or filesystem paths reappear in
  the tree.

### Changed

- Schema `$id` updated from the internal Forgejo URL to the public GitHub raw
  URL (`https://raw.githubusercontent.com/vtmocanu/fj-queue/main/schema/fj-queue.v1.json`).
- All internal host names, Prometheus URLs, Kubernetes namespace names, cluster
  labels, node prefixes, pod names, and org slugs replaced with neutral
  `*.example.com` / `owner-a` / `ci-runner-*` equivalents throughout tests,
  fixtures, and snapshots.
- NCPS status decoupled from metrics toggle: `--ncps` and `--metrics` are now
  independent. Enabling NCPS without metrics (or vice versa) is valid.
- Neutral built-in defaults throughout: empty strings replace private URLs and
  namespace names so the tool is safe to run against any Forgejo instance.

[2.0.0]: https://github.com/vtmocanu/fj-queue/releases/tag/v2.0.0
