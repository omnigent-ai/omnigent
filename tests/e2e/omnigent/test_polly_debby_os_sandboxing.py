"""OS-level filesystem isolation for the bundled Polly and Debby agents."""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from omnigent.inner.os_env import create_os_environment
from omnigent.spec import load

_REPO = Path(__file__).resolve().parents[3]
_SANDBOX_SYSTEM_ROOTS = (Path("/usr"), Path("/bin"), Path("/sbin"))


def _extra_git_read_paths() -> list[str]:
    """Return non-system git binary roots needed by this host's sandbox."""
    git_paths = {
        Path(path).resolve().parent
        for path in (shutil.which("git"), shutil.which("git", path=os.defpath))
        if path is not None
    }
    return [
        str(path)
        for path in sorted(git_paths)
        if not any(path == root or path.is_relative_to(root) for root in _SANDBOX_SYSTEM_ROOTS)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle_name", ["polly", "debby"])
async def test_bundle_shell_cannot_read_host_file_outside_workspace(
    bundle_name: str,
    tmp_path: Path,
) -> None:
    """A real sandboxed shell can use the workspace but cannot read its sibling."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    visible = workspace / "visible.txt"
    visible.write_text("workspace-visible", encoding="utf-8")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("must-not-leak", encoding="utf-8")

    spec = load(_REPO / "examples" / bundle_name)
    assert spec.os_env is not None
    assert spec.os_env.sandbox is not None
    sandbox = replace(spec.os_env.sandbox, read_paths=[str(_REPO)])
    os_env = create_os_environment(replace(spec.os_env, cwd=str(workspace), sandbox=sandbox))
    assert os_env is not None
    try:
        control = await os_env.shell("cat visible.txt")
        escaped = await os_env.shell(f"cat {shlex.quote(str(secret))}")
    finally:
        os_env.close()

    assert control.get("stdout") == "workspace-visible"
    assert control.get("exit_code") == 0
    assert "must-not-leak" not in escaped.get("stdout", "")
    assert escaped.get("exit_code") != 0


@pytest.mark.asyncio
async def test_polly_sandbox_supports_git_worktree_orchestration(tmp_path: Path) -> None:
    """Polly can still create its registry and isolated coding worktrees."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    spec = load(_REPO / "examples" / "polly")
    assert spec.os_env is not None
    assert spec.os_env.sandbox is not None
    sandbox = replace(
        spec.os_env.sandbox,
        read_paths=[str(_REPO), *_extra_git_read_paths()],
    )
    os_env = create_os_environment(replace(spec.os_env, cwd=str(workspace), sandbox=sandbox))
    assert os_env is not None
    try:
        result = await os_env.shell(
            "git init -q && "
            "git -c user.name=Polly -c user.email=polly@example.com "
            "commit -q --allow-empty -m init && "
            "mkdir -p .polly && printf '{}\\n' > .polly/registry.json && "
            "git worktree add -q .worktrees/task -b polly/task && "
            "git -C .worktrees/task status --short"
        )
    finally:
        os_env.close()

    assert result.get("exit_code") == 0, result
    assert (workspace / ".polly" / "registry.json").is_file()
    assert (workspace / ".worktrees" / "task" / ".git").is_file()
