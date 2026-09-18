"""The hook interpreter flag must keep cwd off sys.path without dropping PYTHONPATH."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from omnigent.util.hook_python import SAFE_PATH_FLAG


def _package(root: Path) -> None:
    """Write an importable ``pkg.sub.mod`` under *root*."""
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "sub" / "__init__.py").write_text("")
    (root / "pkg" / "sub" / "mod.py").write_text("print('hook ran')\n")


def _run(cwd: Path, pythonpath: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin"}
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, SAFE_PATH_FLAG, "-m", "pkg.sub.mod"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_flag_preserves_pythonpath(tmp_path: Path) -> None:
    """A hook module reachable only via PYTHONPATH must still import.

    ``-I`` would discard PYTHONPATH here, which is how native-harness hooks
    died with ``ModuleNotFoundError`` on PYTHONPATH-based and ``--user``
    installs, silently disabling every feature the hooks drive.
    """
    src = tmp_path / "src"
    src.mkdir()
    _package(src)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run(elsewhere, str(src))

    assert result.returncode == 0, result.stderr
    assert "hook ran" in result.stdout


def test_flag_keeps_cwd_off_sys_path(tmp_path: Path) -> None:
    """A workspace that is itself a checkout must not shadow the real package."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _package(workspace)

    result = _run(workspace, None)

    assert result.returncode != 0
    assert "No module named 'pkg'" in result.stderr
