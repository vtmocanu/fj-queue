# Installation

## Requirements

- **Python 3.11 or newer.**
- **[`uv`](https://docs.astral.sh/uv/)** installed on the host (including any
  agent host that shells out to `fj_queue.py`). Runtime libraries (`rich`,
  `httpx`) are declared in a PEP-723 inline header; `uv run` resolves and
  caches them on first invocation. No manual venv or `pip install` needed.
- **An admin-scoped Forgejo API token.** The dashboard talks to
  `/api/v1/admin/actions/runners` and `/api/v1/admin/actions/runners/jobs`,
  which require `is_admin: true`. A non-admin token returns HTTP 403 and the
  tool exits 3.

## Install with Homebrew (recommended)

```bash
brew tap vtmocanu/tap
brew install fj-queue
```

The formula pulls in `uv` and a pinned Python automatically, so the only
remaining requirement is an admin-scoped Forgejo token (below). Invoke it as
`fj-queue`:

```bash
fj-queue --host git.example.com
```

`brew upgrade fj-queue` moves to the latest release.

## Running from a checkout (no install)

Clone or copy `fj_queue.py` to any directory, then:

```bash
uv run fj_queue.py --host git.example.com
```

Or use the bundled `fj-queue` symlink (if you cloned the full repo):

```bash
./fj-queue --host git.example.com
```

Both forms are equivalent. The symlink calls `uv run fj_queue.py` under the
hood.

## Token setup

Pass the token on the command line:

```bash
uv run fj_queue.py --host git.example.com --token <your-token>
```

Or export it as an environment variable (recommended for scripts):

```bash
export FORGEJO_TOKEN=<your-token>
uv run fj_queue.py --host git.example.com
```

The token is never written to a config file. See
[Configuration](configuration.md) for the full precedence rules.
