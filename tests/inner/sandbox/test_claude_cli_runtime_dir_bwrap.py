"""The Linux default CLI sandbox keeps the Claude CLI's ``/tmp/claude-<uid>``
runtime dir writable once it is granted as an explicit write root.

bwrap mounts a private tmpfs over ``/tmp``, so the grant only takes effect if
the host directory is bound on top of it. Runs only where bwrap can create a
namespace (CI's offline sandbox lane) and skips elsewhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.inner.claude_sdk_executor import _claude_internal_write_roots
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import (
    create_exec_launcher,
    resolve_sandbox,
    with_additional_write_roots,
)
from tests.inner.sandbox.conftest import _bwrap_functional, _repo_root_for_pythonpath

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="linux_bwrap requires Linux + bwrap on PATH",
)


def test_claude_cli_tmp_runtime_dir_writable_under_bwrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not _bwrap_functional():
        pytest.skip("bwrap cannot create a user namespace on this host")

    # A non-/tmp system tempdir makes the /tmp-anchored root a distinct extra root,
    # as on a host with a custom $TMPDIR; HOME keeps ~/.claude out of the real home.
    system_tmp = tmp_path / "systmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    monkeypatch.setenv("TMPDIR", str(system_tmp))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # A unique root under the real /tmp keeps bwrap's tmpfs-over-/tmp ordering in
    # play without touching the shared /tmp/claude-<uid> of a live CLI.
    runtime_root = Path(tempfile.mkdtemp(prefix="claude-cli-root-", dir="/tmp"))
    runtime_dir = runtime_root / f"claude-{os.getuid()}"
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            # Repo root so the launcher can import omnigent inside the sandbox.
            read_paths=[_repo_root_for_pythonpath()],
            write_paths=[],
            allow_network=True,
        ),
    )
    launcher: str | None = None
    probe = runtime_dir / f"probe-{os.getpid()}"
    try:
        with patch("omnigent.inner.claude_sdk_executor._CLAUDE_CLI_TMP_ROOT", runtime_root):
            roots = _claude_internal_write_roots()
        sandbox = with_additional_write_roots(resolve_sandbox(spec, workspace), roots)
        assert runtime_dir.is_dir(), f"{runtime_dir} was not created by the grant"
        launcher = create_exec_launcher("/bin/sh", sandbox)
        result = subprocess.run(
            [launcher, "-c", f"printf ok > {probe} && cat {probe}"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        # The write reached the host directory, not bwrap's private /tmp.
        assert probe.read_text() == "ok"
    finally:
        if launcher is not None:
            Path(launcher).unlink(missing_ok=True)
        shutil.rmtree(runtime_root, ignore_errors=True)
