# Agent team workflow for fj-queue

Generated 2026-05-30 by the `agent-team` skill.

## Team roster

| Role | Subagent type | Model | Tools |
|------|---------------|-------|-------|
| coder | coder | sonnet | (inherit all) |
| reviewer | reviewer | sonnet | Bash, Read, Grep, Glob, WebFetch, + team-coord |
| auditor | auditor | sonnet | Bash, Read, Grep, Glob, WebFetch, + team-coord |
| tester | tester | sonnet | Bash, Read, Grep, Glob, WebFetch, + team-coord |
| documenter | documenter | sonnet | Bash, Read, Grep, Glob, Edit, Write, WebFetch, + team-coord |
| release | release | sonnet | Bash, Read, Grep, Glob, + team-coord |

(team-coord = SendMessage, TaskUpdate, TaskList, TaskGet)

## Orchestrator workflow

You (the team lead) NEVER do implementation, review, audit, test, doc, or
release work yourself. You coordinate the team via TeamCreate + Agent (with
team_name + name + subagent_type) + SendMessage + TaskUpdate.

Default flow for a typical task:
1. Spawn coder with the full task context. The coder runs
   `uv run --frozen --group dev pytest tests/` before reporting done.
2. After coder reports done, spawn reviewer + auditor + tester IN PARALLEL with
   the coder's diff + report. (tester re-runs the suite and adds coverage;
   reviewer checks correctness/contract; auditor checks token/HTTP safety.)
3. Resolve any blocking findings (route them back to coder via SendMessage).
4. If the change touched user-facing behavior, flags, or output, dispatch
   documenter to sync README/CONTRIBUTING.
5. Before delegating to release, summarize what to verify end-to-end and STOP
   for user confirmation (a tag push is an irreversible shared-system write).
6. On user OK, spawn release for the manual version-tag flow.

## Context handoff (CRITICAL)

Every teammate cold-starts with no memory of prior conversation or other
teammates' outputs. Whatever you write in the spawn `prompt:` is the entire
context they have, plus the body of `.claude/agents/<role>.md`.

Therefore every spawn prompt MUST include:
- File paths the teammate should read (the task/spec, `fj_queue.py`, the test
  files being touched, CONTRIBUTING.md when authoring rules matter).
- A summary of any prior teammate's findings when chaining workers.
- The exact error message / failing test output when retrying after a failure.
- If context is long, write it to `.claude/agent-team-tasks/<slug>.md` and
  reference that path in the prompt instead of pasting inline.

## Project signals

- Language/runtime: Python >= 3.11, single module `fj_queue.py` (repo root);
  `fj-queue` is a symlink to it.
- Package manager: `uv` (`pyproject.toml`, `uv.lock`).
- Test command: `uv run --frozen --group dev pytest tests/`
- Snapshot update: `uv run --group dev pytest tests/ --snapshot-update`
- Test stack: pytest 8.3.4 + syrupy (snapshots, `tests/__snapshots__/`) +
  respx (httpx mocking) + jsonschema.
- Stability surfaces: JSON contract `schema/fj-queue.v1.json` guarded by
  `tests/test_json_contract.py`; syrupy snapshots for CLI/render output.
- Release flow: manual version-tag (bump `pyproject.toml` version, tag
  `vX.Y.Z`, push, optional `gh release create`). No release CI, no CHANGELOG.
  Latest tag: v1.0.0. Use `gh` (GitHub repo, not Forgejo; never `tea`).
- Spec dir: none.
- Authoring rules: CONTRIBUTING.md (no CLAUDE.md in repo). Also CODE_OF_CONDUCT.md,
  SECURITY.md.
- CI: GitHub Actions `.github/workflows/test.yml` (pytest matrix 3.11-3.13,
  SHA-pinned actions, persist-credentials false). No secret scanner.
- Repo: public, github.com/vtmocanu/fj-queue.
- Slash commands the orchestrator may invoke between delegations: none project-
  local; global dot-ai skills available (e.g. /code-review, /security-review).
