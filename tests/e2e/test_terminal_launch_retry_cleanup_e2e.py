"""The runner's resource facade can retry a failed launch without orphaned panes."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult, TerminalInstance
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals import TerminalRegistry, registry


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize(
    "role", ["claude-native", "codex-native", None], ids=["claude", "codex", "shell"]
)
@pytest.mark.timeout(30)
async def test_terminal_retry_after_spawn_failure_has_no_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    instances: list[TerminalInstance] = []
    original_create = registry.create_terminal_instance

    def create(*args: Any, **kwargs: Any) -> TerminalCreateResult:
        created = original_create(*args, **kwargs)
        instances.append(created.instance)
        return created

    monkeypatch.setattr(registry, "create_terminal_instance", create)
    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminals, runner_workspace=tmp_path)
    launch = resources.launch_required_terminal if role else resources.launch_auxiliary_terminal
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    spec = TerminalEnvSpec(
        command="cat",
        os_env=OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none")),
    )
    failed_spec = TerminalEnvSpec(command="cat", env={"PATH": str(empty_bin)}, os_env=spec.os_env)
    session_id = "owned-retry-session"
    try:
        for _ in range(3):
            with pytest.raises(FileNotFoundError):
                await launch(session_id, "terminal", "main", failed_spec, resource_role=role)
            assert instances, "create_terminal_instance was never intercepted"
            assert all(not instance.private_dir.exists() for instance in instances)
            assert resources.list_resources(session_id, resource_type="terminal").data == []

        view = await launch(session_id, "terminal", "main", spec, resource_role=role)
        assert view.metadata["running"] is True
        assert len(resources.list_resources(session_id, resource_type="terminal").data) == 1
        instance = terminals.get(session_id, "terminal", "main")
        assert instance is not None
        assert (await instance.send(text="CLEAN_RETRY_MARKER"))["status"] == "sent"
        async with asyncio.timeout(5):
            while "CLEAN_RETRY_MARKER" not in (await instance.read())["screen"]:
                await asyncio.sleep(0.05)
        await resources.cleanup_session(session_id)
        assert all(not instance.private_dir.exists() for instance in instances)
        assert terminals.active_conversation_ids() == []
    finally:
        with contextlib.suppress(Exception):
            await resources.cleanup_session(session_id)
        for instance in instances:
            with contextlib.suppress(Exception):
                await instance.close()
