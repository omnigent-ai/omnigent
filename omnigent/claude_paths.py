"""Claude-compatible config paths without provider or settings imports."""

from __future__ import annotations

import os
from pathlib import Path


def claude_joined_path(cwd: Path | None, directory: str, name: str) -> Path:
    """Join a Claude config path lexically before filesystem symlinks are followed."""
    joined = os.path.normpath(os.path.join(directory, name))
    if os.path.isabs(joined):
        return Path(joined)
    return (cwd if cwd is not None else Path.cwd()) / joined


def claude_account_home_dir() -> Path:
    """Resolve the account home Claude uses when its launch environment has no HOME."""
    try:
        import pwd  # noqa: FlagLocalImports - POSIX-only module

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()
