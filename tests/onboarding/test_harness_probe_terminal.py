"""Readiness probes must not change the calling terminal's input mode."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX PTY")
@pytest.mark.parametrize("probe", ["harness_cli_installed", "harness_cli_logged_in"])
def test_readiness_probes_leave_terminal_input_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    import pty
    import termios

    cli = tmp_path / "claude"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import sys, tty\n"
        "if sys.stdin.isatty():\n"
        "    tty.setraw(sys.stdin.fileno())\n"
        "print('9.9.9' if '--version' in sys.argv else '{\"loggedIn\": true}')\n"
    )
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    master, slave = pty.openpty()
    try:
        original = termios.tcgetattr(master)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from omnigent.onboarding import harness_install as hi; "
                f"assert hi.{probe}('anthropic')",
            ],
            stdin=slave,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert termios.tcgetattr(master) == original
    finally:
        os.close(slave)
        os.close(master)
