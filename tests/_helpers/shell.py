"""
Check how a POSIX shell splits a command a user pastes from CLI output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def pasted_shell_words(command: str, cwd: Path) -> list[str]:
    """
    Return the words ``bash -O failglob`` passes when *command* is pasted.

    ``failglob`` aborts on an unmatched glob, like zsh's default ``nomatch``.

    :param command: Command text as printed, e.g. ``"tool login https://x?o=1"``.
    :param cwd: Directory the shell expands globs in, e.g. an empty ``tmp_path``.
    :returns: The argument words, e.g. ``["tool", "login", "https://x?o=1"]``.
    :raises AssertionError: If the shell rejects the pasted command.
    """
    result = subprocess.run(
        ["bash", "-O", "failglob", "-c", f'printf "%s\\n" {command}'],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, f"pasting {command!r} failed: {result.stderr.strip()}"
    return result.stdout.splitlines()
