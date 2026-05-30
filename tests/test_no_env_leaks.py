"""Guard test: repo must contain no environment-specific literals.

Walks the entire repository tree and fails if any of the forbidden patterns
(internal host names, Prometheus URL, real cluster/pod/node identifiers,
maintainer filesystem paths) appear anywhere outside of known exception paths.

Exceptions (legitimately contain old values for documentation/history):
  - .git/ (git history; scrubbed separately in M7)
  - .venv/ (third-party packages)
  - __pycache__/ (bytecode)
  - .pytest_cache/
  - .claude/agent-team-tasks/ (internal working notes -- not shipped)
  - prds/ (PRD documents -- describe old state for context)
  - README.md, docs/ (M5 scope; documenter-2 cleans these concurrently)

If this test fails ONLY on README.md or docs/, that is expected while M5 is
in progress and does not block M4.
"""

from __future__ import annotations

import re
from pathlib import Path


# Root of the repository (parent of the tests/ directory).
REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories excluded from scanning (relative to REPO_ROOT).
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".claude",       # agent-team-tasks working notes live here
    "prds",          # PRD documents legitimately describe old state
}

# Files excluded from scanning (relative to REPO_ROOT).
# This file is excluded so its own pattern-string literals don't self-trigger.
EXCLUDED_FILES = {
    "test_no_env_leaks.py",  # exclude self (pattern defs would self-trigger)
}

# Directories (relative to REPO_ROOT) whose paths start with a prefix below
# are excluded from scanning.
EXCLUDED_DIR_PREFIXES: tuple[str, ...] = ()

# Patterns that must NOT appear in shipped code.
# Each entry is (pattern_str, description).
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"git\.wxs\.ro", "internal Forgejo host"),
    (r"prometheus\.wxs\.ro", "internal Prometheus host"),
    (r"nix-cache\.wxs\.ro", "internal NCPS host"),
    (r"k8s-node-", "internal blue/green node prefix"),
    (r"k8s-node-", "internal blue/green node prefix"),
    (r"\bk8s-cluster\b", "internal cluster label value"),
    (
        r"forgejo-runner-[0-9a-f]+-[a-z0-9]{5}",
        "real runner pod name (Deployment hash + suffix)",
    ),
    (r"/home/user/", "maintainer local filesystem path"),
    (r"fj-queue", "internal task tracker path"),
]

# The 'owner-a' pattern: match only as an org login/slug (lowercase,
# as part of an org/repo path or login field) but not as a project keyword
# in prose (e.g. "Crossplane" with capital C is the OSS project name and
# may appear in documentation).
FORBIDDEN_PATTERNS.append((r"\"owner-a\"", "internal org slug as JSON string literal"))
FORBIDDEN_PATTERNS.append((r"owner-a/", "internal org slug in repo path"))
FORBIDDEN_PATTERNS.append((r"\bcontainers/", "real 'containers' org slug in repo path"))


def _should_skip(path: Path) -> bool:
    """Return True if path is inside an excluded directory or is an excluded file."""
    rel = path.relative_to(REPO_ROOT)
    parts = rel.parts

    # Check excluded top-level dirs.
    if parts[0] in EXCLUDED_DIRS:
        return True

    # Check excluded files.
    if rel.name in EXCLUDED_FILES:
        return True

    # Check excluded dir prefixes (M5 scope).
    rel_str = str(rel)
    for prefix in EXCLUDED_DIR_PREFIXES:
        if rel_str.startswith(prefix):
            return True

    return False


def _collect_text_files() -> list[Path]:
    """Yield all text files in the repo tree that should be scanned."""
    result = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if _should_skip(path):
            continue
        # Skip binary files by attempting UTF-8 decode.
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        result.append(path)
    return sorted(result)


def test_no_environment_specific_literals():
    """Fail if any forbidden pattern is found in the repository tree."""
    compiled = [(re.compile(pat), desc) for pat, desc in FORBIDDEN_PATTERNS]
    files = _collect_text_files()

    violations: list[str] = []
    for fpath in files:
        content = fpath.read_text(encoding="utf-8")
        rel = fpath.relative_to(REPO_ROOT)
        for regex, desc in compiled:
            for match in regex.finditer(content):
                # Find the line number.
                lineno = content[: match.start()].count("\n") + 1
                violations.append(
                    f"{rel}:{lineno}: [{desc}] matched {match.group()!r}"
                )

    if violations:
        msg = "Environment-specific literals found in repo tree:\n" + "\n".join(
            f"  {v}" for v in violations
        )
        # Surface the count separately for easy scanning.
        msg += f"\n\nTotal violations: {len(violations)}"
        raise AssertionError(msg)
