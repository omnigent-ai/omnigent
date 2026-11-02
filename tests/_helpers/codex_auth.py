"""Local executables and subprocess recording for Codex authentication tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def token_helper_environment(
    *,
    home: Path,
    tools: Path,
    source_root: Path,
    helper_paths: Sequence[Path],
    argument_log: Path,
    token: str,
) -> dict[str, str]:
    """Install synthetic token helpers in an isolated, credential-free environment."""
    home.mkdir()
    tools.mkdir()
    for name in ("sh", "node", "env"):
        executable = shutil.which(name)
        if executable is None:
            raise FileNotFoundError(name)
        (tools / name).symlink_to(executable)
    package = home / "test package" / "ucode"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import json, os, sys\n"
        "def main():\n"
        "    with open(os.environ['TEST_HELPER_LOG'], 'a') as log:\n"
        "        log.write(json.dumps(sys.argv) + '\\n')\n"
        f"    print({token!r})\n"
    )
    for helper in helper_paths:
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text(f"#!{sys.executable}\nfrom ucode.cli import main\nmain()\n")
        helper.chmod(0o755)
    return {
        "HOME": str(home),
        "PATH": str(tools),
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(package.parent)]
            + [str(source_root / relative) for relative in (".", "sdks/python-client", "sdks/ui")]
        ),
        "TEST_HELPER_LOG": str(argument_log),
    }


def run_recorded(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and retain its output without deciding its expected result."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )
    log.write_text(result.stdout + result.stderr)
    return result


def run_codex_turn(
    *,
    executable: str,
    overrides: Sequence[str],
    prompt: str,
    cwd: Path,
    environment: dict[str, str],
    log: Path,
) -> subprocess.CompletedProcess[str]:
    """Run real Codex against caller-supplied config without ambient user input."""
    command = [executable, "exec", "--skip-git-repo-check", "--ephemeral", "--json"]
    for override in overrides:
        command.extend(["-c", override])
    return run_recorded([*command, prompt], cwd=cwd, environment=environment, log=log, timeout=60)
