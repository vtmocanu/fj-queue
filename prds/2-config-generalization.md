# PRD #2: config file + optional environment-specific features

**Issue**: [#2](https://github.com/vtmocanu/fj-queue/issues/2)
**Priority**: Medium
**Status**: M1-M6 implemented on branch `feature/prd-2-config-generalization` (PR [#6](https://github.com/vtmocanu/fj-queue/pull/6)); version 2.0.0; full suite green (327 passed). Remaining post-merge: tag the `v2.0.0` release, then M7 (git-history scrub). Generalizes the tool (whose initial implementation and Prometheus/NCPS metrics already shipped) for public consumption.

## Problem

fj-queue was built for one specific environment and bakes that environment into the shipped code:

- The Forgejo host, the Prometheus metrics URL, the runner namespace, and a blue/green cluster node-prefix split are all hardcoded as argparse defaults and string literals.
- The same environment-specific references appear in code comments (live-verification provenance), the README, and test fixtures/snapshots (captured against the maintainer's host, including real `owner/repo` slugs and node names).
- The Prometheus runner-pod CPU/memory metrics and the NCPS (nix cache proxy) cache-status feature are only meaningful inside the maintainer's Kubernetes cluster, yet they run by default.

Consequences: the public repo carries environment-specific references, every external user must override defaults to use the tool at all, and the metrics/NCPS sections produce noise for anyone outside that cluster.

## Solution

1. **Config file with layered resolution.** A config file parsed with the stdlib `tomllib` (no new runtime dependency). Per-setting precedence, highest wins: **CLI flag > environment variable > config file > built-in neutral default.** The config file itself is discovered in order, following the convention in [vtmocanu/git-manager](https://github.com/vtmocanu/git-manager): `--config PATH` → `./fj-queue.toml` (current directory) → `$XDG_CONFIG_HOME/fj-queue/config.toml` (defaults to `~/.config/fj-queue/config.toml` when `XDG_CONFIG_HOME` is unset). TOML rather than git-manager's YAML because `tomllib` is stdlib and YAML would add a runtime dependency. The API token stays env/flag only and is never read from or written to the config file (it is a secret).
2. **Prometheus metrics optional, default OFF.** The per-pod runner CPU/memory section is opt-in (`metrics.enabled = true` / `--metrics`). Default-off reuses the existing graceful-degradation contract (`runner_pods: []`, `metrics.error: "disabled"`).
3. **NCPS cache status optional, default OFF, and independent of metrics.** Same opt-in model (`ncps.enabled = true` / `--ncps`), but decoupled from the metrics toggle (see Design notes — they are currently coupled in code).
4. **Remove environment-specific references.** No hardcoded host / Prometheus URL / runner namespace / cluster node-prefix strings, and no real `owner/repo` slugs or node names, in shipped code, comments, docs, fixtures, or snapshots. Built-in defaults are neutral or empty; fixtures/snapshots regenerate against a neutral example host (`git.example.com`) and anonymized slugs. A guard test fails if environment-specific patterns reappear.
5. **README + docs refactor.** Restructure the README to a terse launchpad (badges, Quick Start, a quick example/config, hero screenshot, link to docs) and split the detailed reference into an in-repo `docs/` folder. A blurred hero screenshot of the TUI is already prepared.

### Out of scope

- New data sources or dashboard features (this is generalization, not expansion).
- Homebrew distribution (separate track).
- Multi-profile config (multiple hosts in one file) — deferred unless a use case lands.

## Design notes

- **Config schema (TOML), example values only:**
  ```toml
  host = "git.example.com"           # no private default ships

  [metrics]                          # section absent / enabled=false ⇒ off
  enabled = true
  url = "https://prometheus.example.com"
  namespace = "ci-runners"
  node_prefix = "k8s-node-"          # free-form; replaces the old hardcoded enum

  [ncps]                             # independent of [metrics]
  enabled = true
  ```
- **Host resolution:** if no host is provided by flag, env (`FORGEJO_HOST`), or config, exit 2 with a message pointing at `--host` / `$FORGEJO_HOST` / the config file. No private host is ever the silent default. `EXIT_USAGE`/`ConfigError` and the `resolve_token` env-precedence helper already exist; add a `resolve_host` mirror.
- **NCPS/metrics decoupling (required — they are coupled today).** The NCPS fetch currently lives *inside* the `if config.metrics_enabled:` branch in `_do_one_fetch`, and there is no `ncps_enabled` field or `--ncps` flag. M3 must add `Config.ncps_enabled`, a `--ncps` flag, restructure `_do_one_fetch` so NCPS fetches independently of `metrics_enabled`, and give NCPS its own disabled representation — today `ncps: null` is overloaded (means metrics-disabled *and* NCPS-fetch-failed); the `--ncps`-off case needs to be distinguishable (or the overload must be documented as intentional).
- **Cluster blue/green generalization.** The current `--metrics-cluster {auto,green,blue}` enum and the hardcoded `k8s-{cluster}-` node-prefix filter are environment-specific. Replace the enum with a free-form `node_prefix` string (config/flag); update the `--metrics-cluster`/`--node-prefix` help text and the `PodResource` schema field description (which still names the old prefixes).
- **Backward-compatible flags:** existing flags keep working as the highest-precedence layer; `--no-metrics` becomes redundant with default-off but stays as an explicit override. Add `--metrics` / `--ncps` opt-in flags and `--config`.
- **JSON contract:** additive only — **no `schema_version` bump**. `to_dict` always emits `runner_pods`/`metrics`/`ncps`, and the schema marks them required with `additionalProperties:false`; default-off populates them with the existing `disabled`/`null` representations, which already validate.

## Milestones

- [x] **M1 — Config file + layered resolution.** `tomllib` loader; config-file discovery order `--config PATH` → `./fj-queue.toml` → `$XDG_CONFIG_HOME/fj-queue/config.toml` (→ `~/.config` fallback), per the git-manager convention; per-setting precedence flag > env > config > neutral default; host required (typed exit 2 with guidance when unset, via a `resolve_host` mirror of `resolve_token`); token kept out of config. Unit tests for precedence and the missing-host path.
- [x] **M2 — Prometheus metrics optional, default OFF.** Gate the runner-pod metrics fetch on config/`--metrics`; default-off reuses the `disabled` degradation path. Generalize the `--metrics-cluster` enum + hardcoded `k8s-{cluster}-` prefix to a free-form `node_prefix`; update help text + the `PodResource` schema description. Revisit the test harness's `_run` helper, which auto-injects `--no-metrics` *because metrics are currently on by default* — that scaffolding and its comment become wrong when the default flips. Tests for off-by-default and opt-in.
- [x] **M3 — NCPS optional, default OFF, decoupled from metrics.** Add `ncps_enabled` + `--ncps`; restructure `_do_one_fetch` so NCPS fetches independently of `metrics_enabled`; give NCPS a distinct disabled representation (don't reuse the metrics `disabled` sentinel / overloaded `ncps:null`). Tests for all states (metrics-on+ncps-off, metrics-off+ncps-on, both, neither).
- [x] **M4 — Remove environment-specific references + guard.** Strip hardcoded host / Prometheus URL / runner namespace / cluster-prefix strings AND real `owner/repo` slugs + node names from code/comments/docs/fixtures/snapshots; regenerate fixtures + Rich/plain/JSON snapshots against `git.example.com` and anonymized slugs (`owner-a/repo-a`, …); keep the trusted-vs-attacker host pair in the cross-host security tests clearly distinct (e.g. trusted `git.example.com`, attacker `attacker.test`). Set the schema `$id` to the GitHub raw URL (not a host swap). Add a guard test whose pattern covers BOTH host tokens and `owner/repo` slug shapes, so "no leaks" is actually enforced. **Scope reality:** ~13 files, including non-mechanical hand-edits (the client tests embed host literals in regexes) plus a snapshot-regeneration step — not a single mechanical commit.
- [x] **M5 — README + docs/ refactor.** Terse house-style README (badges, Quick Start, quick example, hero screenshot, Documentation link), with internal links removed (no PRD/issue links to non-public trackers); `docs/*.md` split (installation, configuration incl. the config-file format + a committed `config.toml.example`, usage, JSON contract, metrics/NCPS opt-in, caveats) + `docs/img/` hero shot.
- [x] **M6 — Tests + schema/docs green + release.** Full suite green across the supported Python matrix; schema doc + `--schema` reflect the optional sections; bump `__version__` + `pyproject` version + add a CHANGELOG entry (behavior change → minor/major); tag the release; CI green. *(Tree work, version bump to 2.0.0, CHANGELOG, and `--schema` landed in PR #6. Tagging the `v2.0.0` release and confirming CI green are deferred to merge.)*
- [ ] **M7 — Scrub git history (after M1-M6 reach main).** Removing the references from the tree does NOT remove them from the public repo's history — the initial commit and every commit since still expose the host, Prometheus URL, runner namespace, node names, and real `owner/repo` slugs via `git log -p`. Rewrite history to redact them with `git filter-repo` (a replacements file mapping each internal string to its neutral equivalent), then force-push all branches + tags. This requires **temporarily lifting the anti-destruction branch protection** (force-push is blocked by default; `gh api -X PUT .../branches/main/protection` with `allow_force_pushes:true`, then restore). Acceptable because the repo is new with no external forks; coordinate (or reconsider) if it has been forked/cloned by then. Verify: `git log -p --all | rg -i '<internal patterns>'` returns nothing, then re-enable protection.

### Phase / dependency analysis

| Milestone | Depends on | Parallelizable |
|---|---|---|
| M1 config loader | — | foundation, first |
| M2 metrics opt-in (+ cluster generalization) | M1 | after M1 |
| M3 NCPS opt-in (+ decoupling) | M1 | after M1; parallel with M2 |
| M4 strip refs + guard | M1-M3 (defaults settle) | after M2/M3; parallel with M5 |
| M5 README + docs | M1-M3 | after defaults settle; parallel with M4 |
| M6 tests/schema/release | M1-M5 | after M4/M5 |
| M7 history scrub | M1-M6 on main | last; one-shot, irreversible |

M1 is the foundation. M2 and M3 are independent once M1 lands (M3 has its own decoupling work). M4 and M5 both depend on settled defaults, then run in parallel. M6 closes out the tree changes; M7 scrubs history only after everything has reached main (rewriting history mid-stream would just have to be redone).

## Success criteria

- A fresh clone with no config and no env runs against a user-supplied `--host` (or `$FORGEJO_HOST`), with metrics and NCPS OFF, producing a clean runner/queue view; a missing host exits 2 with actionable guidance, not a private default.
- A local `~/.config/fj-queue/config.toml` turns metrics + NCPS on (independently) and restores the full dashboard, with no environment values living in the repo.
- The guard test's pattern (host tokens **and** `owner/repo` slug shapes) finds nothing in the tree, and is wired into CI.
- README leads terse (badges, Quick Start, quick example, hero screenshot) and links to `docs/`; detailed reference lives under `docs/`; no internal-only links remain.
- Full test suite green across the supported Python matrix; JSON schema unchanged (`schema_version` not bumped; additive only); schema `$id` points at the public GitHub raw URL.

## Risks & mitigations

- **Snapshot + fixture churn.** Rehosting fixtures/snapshots to `git.example.com` + anonymized slugs touches ~13 files incl. hand-edited host regexes in the client tests. Mitigation: settle defaults (M1-M3) first, then do the strip + regenerate as one focused M4 change; the guard test then locks it.
- **NCPS/metrics coupling.** The existing code fetches NCPS only when metrics are enabled. Mitigation: explicit decoupling in M3 (don't treat it as a parallel no-op of M2).
- **Behavior change for the maintainer.** Default-off metrics/NCPS and a required host change the bare-invocation UX. Mitigation: the maintainer's local (untracked) config restores prior behavior; a committed `config.toml.example` documents it.
- **Binary assets leak.** Screenshots can expose internal hostnames/slugs/node names that grep can't catch. Mitigation: inspect assets before embedding (the hero shot was blurred for this reason).

## Dependencies

- Builds on the tool's existing implementation and its JSON/schema contract.
- Python 3.11+ (`tomllib` is stdlib). No new runtime dependency.
