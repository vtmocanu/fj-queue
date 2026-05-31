# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`fj-queue` is a read-only dashboard for the Forgejo admin Actions API. It polls runners + queued jobs and renders them two ways: a live Rich TUI for humans, and a byte-deterministic JSON document (backed by a committed JSON Schema) for agents/scripts. Public OSS repo on GitHub (`github.com/vtmocanu/fj-queue`), so use `gh` (never `tea`).

## How to work in this repo (agent team)

For any non-trivial unit of work in this repo (a new feature, a bug fix, a refactor, a docs pass, a release), load the `agent-team` skill (`/agent-team`) and spin up the team to do it, rather than working solo. The roster lives in `.claude/agents/` and the workflow in `.claude/agent-team.md`:

- **coder** implements the change and runs the test suite before reporting done.
- **reviewer** reviews the diff for correctness, style, and edge cases (read-only).
- **auditor** audits for security and unsafe patterns (read-only).
- **documenter** updates README / docs / CHANGELOG to match the change.
- **release** runs the version-tag release flow (only after the user confirms; see "Before committing, pushing, or releasing").
- **tester** validates with pytest and adds tests in the existing style.

Default flow: coder implements, then reviewer + auditor run in parallel on the diff, then documenter updates docs; route blocking findings back to coder. Spawn only the roles a given task needs.

**The team lead picks the execution mode per task:**
- **Full agent team** (TeamCreate + named teammates + SendMessage mailbox coordination, per the `agent-team` skill's run mode) for multi-step or long-running work that needs the shared task list, back-and-forth between roles, context recycling, or a user-gated release. Requires `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.
- **Background agents** (the Agent tool with `run_in_background: true`, no team) for one-shot, independent, fire-and-forget tasks such as a parallel review + audit of a finished diff, where each agent reports once and needs no mailbox coordination.

Use the lightest mode that fits; reserve the full team for work that actually needs coordination.

## Commands

```bash
uv sync --group dev                              # install dev deps
uv run --frozen --group dev pytest tests/        # full suite (mirrors CI exactly)
uv run --group dev pytest tests/test_render.py   # one file
uv run --group dev pytest -k watch               # by keyword
uv run --group dev pytest --snapshot-update      # regenerate syrupy snapshots after intentional render changes
uv run fj_queue.py --host git.example.com        # run from checkout (or ./fj-queue symlink)
```

CI (`.github/workflows/test.yml`) runs `pytest` on Python 3.11/3.12/3.13 with `--frozen`; keep `uv.lock` in sync. There is no separate lint step.

## Architecture

Everything lives in one file, **`fj_queue.py`** (~3200 lines). It is both an importable module and a PEP-723 self-contained script (`#!/usr/bin/env -S uv run --script`). It is organized into strict layers, marked `M1`..`M6` in section-header comments, and the layering is load-bearing:

- **Config + resolution** (`Config` frozen dataclass; `resolve_token` / `resolve_host` / `resolve_metrics_url` / `load_file_config`). Precedence everywhere is **CLI flag > env var > config file > built-in default**. Two hard rules: no private host/URL ships as a default (omitting a required one exits 2), and **the API token is NEVER read from the config file** (token comes only from `--token` / `$FORGEJO_TOKEN`).
- **`Client` (M1)** — the *only* layer that knows Forgejo URL paths and field names. Forgejo's raw field names are mapped to stable internal names on frozen dataclasses (`Runner`, `RawJob`) here and nowhere else. `fetch_runners` pages via the RFC-5988 `Link` header (with same-host enforcement on each `next` URL and a hard pagination cap); `fetch_jobs` is a single call; `resolve_repo` is per-process cached.
- **Metrics + NCPS clients** — deliberately isolated, separate no-auth httpx client hitting Prometheus (`fetch_runner_pods`, `fetch_ncps_status`). Opt-in (OFF by default since 2.0.0) with graceful degradation: any failure surfaces as an `error`/`*_error` field rather than aborting the snapshot.
- **`aggregate()` (M2)** — **pure: no I/O, no module state, clock is injected** (`now` param). Turns runners + jobs into a `Snapshot`. Correctness anchors live in comments here: per-runner *superset* label matching, `online = active|idle`, FIFO-approximate ordering, and `needs`-gated jobs excluded from `unschedulable`. When touching scheduling logic, read those comments first.
- **JSON layer (M3)** — `to_dict` / `render_json` / `render_json_error`. A second internal→wire field-name remap happens here. Output must stay byte-deterministic (sorted/stable ordering) because agents and snapshot tests depend on it. `schema/fj-queue.v1.json` is the committed contract; `schema_version` (wire) is distinct from `__version__` (tool).
- **Renderers (M4)** — `render_plain` and `render_rich`, both consuming a `Snapshot` *only* (no recomputation, no I/O). Rich render changes ripple into syrupy snapshots.
- **Watch loop (M5)** — `run_watch`: sync poll wrapped in `rich.live.Live(screen=True)`. The fetch fn, sleep, and clock are all injectable so tests drive N ticks deterministically without real time or network.
- **CLI (M6)** — `_build_parser` / `main`, argparse + stdlib only. Wires the layers and dispatches mode (snapshot vs watch) and format (plain/rich/json).

Data flow: `Config → Client.fetch_* → aggregate() → Snapshot → {render_json | render_plain | render_rich}`.

## Conventions that bite

- **Exit codes are contract.** Typed `FjQueueError` subclasses each carry the exit code: 2 config, 3 auth (non-admin token), 4 connection, 5 schema drift; 0 success; 130 on Ctrl-C. `exit_code_for()` maps exceptions. Don't invent new bare `sys.exit` codes.
- **Tokens are scrubbed** from error output (`_scrub_token`). Keep that path intact when adding error messages.
- **Three things to bump on release**, kept in lockstep: `__version__` in `fj_queue.py`, `version` in `pyproject.toml`, and an entry in `CHANGELOG.md`. Bump `schema_version` *only* on a breaking JSON-contract change.
- Snapshot tests (`tests/__snapshots__/test_render.ambr`) use **syrupy**; HTTP is mocked with **respx**; the JSON contract is validated against the schema with **jsonschema**. `tests/test_no_env_leaks.py` fails CI if env vars leak into output — be careful adding anything that reads `os.environ`.

## Before committing, pushing, or releasing

Always pause and ask the user to test the change locally against a real Forgejo instance first. Do not commit/push/tag on their behalf until they confirm it works. Give them the local (not Homebrew) command, pointed at the feature they changed:

```bash
FORGEJO_TOKEN=<admin-token> uv run fj_queue.py --host git.example.com
# add the flags that exercise the new feature, e.g. --mode watch / --format json
```

Running from the checkout (`uv run fj_queue.py`) tests the working-tree code; a Homebrew-installed `fj-queue` runs the last released version, so it will not reflect uncommitted changes.

## Release

Tagging `v*` triggers `.github/workflows/release.yml`: it cuts a GitHub Release (auto notes) and regenerates + pushes the Homebrew formula to `vtmocanu/homebrew-tap`. The generic render-and-publish mechanics live in the reusable `homebrew-tap.yml` in `github.com/vtmocanu/task`, included by `Taskfile.yml` (`task brew:formula` / `brew:publish`, using `HOMEBREW_TAP_TOKEN`); this repo owns only the formula body in `Formula.rb.tmpl` (placeholders `@@URL@@` / `@@SHA256@@`). CI sets `TASK_X_REMOTE_TASKFILES=1` and runs `task --yes` because the include is fetched over https. The release flow never touches source logic.

The releaser must ensure the GitHub Release page for the tag carries the CHANGELOG entry for that version, not just the workflow's auto-generated commit notes. Before tagging: rename the `CHANGELOG.md` `[Unreleased]` section to the version being cut (with the date), and after the release publishes, confirm the Release page body contains that changelog section (edit the release to prepend it if the auto-notes alone landed).
