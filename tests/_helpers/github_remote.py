"""A checkout whose ``git@github.com`` origin is a local bare repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

# Serves ``git@github.com:<path>`` from the bare repositories under its first argument.
GITHUB_SSH = """#!/bin/sh
cd "$1" || exit 1
for remote_command; do :; done
exec sh -c "$remote_command"
"""


def git(*args: str, cwd: Path | None = None) -> str:
    """Run git and return its stripped stdout."""
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    )
    return completed.stdout.strip()


def github_checkout(worktree: Path, remotes: Path, repository: str, branch: str) -> Path:
    """Create *worktree* on *branch*, already pushed once, and return its bare remote.

    Pushes travel over ``core.sshCommand``, so git's status still names GitHub.
    """
    remote = remotes / f"{repository}.git"
    git("init", "-q", "--bare", str(remote))
    git("config", "core.logAllRefUpdates", "always", cwd=remote)
    ssh = remotes / "github-ssh"
    ssh.write_text(GITHUB_SSH)
    ssh.chmod(0o755)
    git("init", "-q", "-b", branch, str(worktree))
    for key, value in (
        ("user.name", "Contributor"),
        ("user.email", "contributor@example.com"),
        ("core.sshCommand", f"{ssh} {remotes}"),
        ("ssh.variant", "simple"),
    ):
        git("config", key, value, cwd=worktree)
    git("remote", "add", "origin", f"git@github.com:{repository}.git", cwd=worktree)
    git("commit", "-q", "--allow-empty", "-m", "Start", cwd=worktree)
    git("push", "-q", "-u", "origin", branch, cwd=worktree)
    return remote
