"""Reset fences survive session reuse and finish private-instance cleanup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult
from omnigent.terminals import registry as registry_module
from omnigent.terminals.registry import TerminalLaunchSupersededError, TerminalRegistry
from tests.runner.helpers import RunningFlagTerminalInstance


@pytest.mark.parametrize("reset_first", [False, True])
def test_deleted_launch_fence_cannot_reappear_after_session_reuse(reset_first: bool) -> None:
    registry = TerminalRegistry()
    old = registry.launch_fence("session-a")
    assert old()
    if reset_first:
        registry.supersede_inflight_launches("session-a")
        assert not old()
    registry.drop_launch_generation("session-a")
    fresh = registry.launch_fence("session-a")
    assert fresh()
    assert not old()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["reset-and-reuse", "cleanup"])
async def test_reset_and_session_reuse_cannot_make_old_launch_adopt_fresh_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    registry = TerminalRegistry()
    started, release = asyncio.Event(), asyncio.Event()
    created: list[RunningFlagTerminalInstance] = []

    class Terminal(RunningFlagTerminalInstance):
        async def launch(self, *, cwd: Path | None = None) -> None:
            if self is created[0]:
                started.set()
                await release.wait()
            self.running = True

        async def close(self) -> None:
            self.running = False

    def create(name: str, key: str, spec: Any, **kwargs: Any) -> TerminalCreateResult:
        instance = Terminal(
            name=name,
            session_key=key,
            socket_path=tmp_path / f"pane-{len(created)}.sock",
            private_dir=tmp_path / f"pane-{len(created)}",
            running=False,
        )
        created.append(instance)
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(registry_module, "create_terminal_instance", create)
    spec = TerminalEnvSpec(command="bash")
    pending = asyncio.create_task(registry.launch("session-a", "bash", "main", spec))
    try:
        await asyncio.wait_for(started.wait(), 5)
        if invalidation == "reset-and-reuse":
            registry.supersede_inflight_launches("session-a")
            registry.drop_launch_generation("session-a")
        else:
            await registry.cleanup_conversation("session-a")
        fresh = await registry.launch("session-a", "bash", "main", spec)
        release.set()
        with pytest.raises(TerminalLaunchSupersededError):
            await asyncio.wait_for(pending, 5)
        assert registry.get("session-a", "bash", "main") is fresh is created[1]
        assert fresh.running
        assert not created[0].running
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await registry.cleanup_conversation("session-a")


@pytest.mark.asyncio
async def test_cancellation_during_superseded_private_close_still_reclaims_the_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = TerminalRegistry()
    started, release = asyncio.Event(), asyncio.Event()
    closing, close_release = asyncio.Event(), asyncio.Event()
    close_cancelled = False

    class Terminal(RunningFlagTerminalInstance):
        async def launch(self, *, cwd: Path | None = None) -> None:
            started.set()
            await release.wait()
            self.running = True

        async def close(self) -> None:
            nonlocal close_cancelled
            closing.set()
            try:
                await close_release.wait()
            except asyncio.CancelledError:
                close_cancelled = True
                raise
            self.running = False

    instance = Terminal(
        name="bash",
        session_key="main",
        socket_path=tmp_path / "old.sock",
        private_dir=tmp_path / "old",
        running=False,
    )
    monkeypatch.setattr(
        registry_module,
        "create_terminal_instance",
        lambda *args, **kwargs: TerminalCreateResult(instance=instance, cwd=tmp_path),
    )
    pending = asyncio.create_task(
        registry.launch("session-a", "bash", "main", TerminalEnvSpec(command="bash"))
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        registry.supersede_inflight_launches("session-a")
        release.set()
        await asyncio.wait_for(closing.wait(), 5)
        pending.cancel()
        close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
        assert not close_cancelled
        assert not instance.running
        assert registry.get("session-a", "bash", "main") is None
    finally:
        release.set()
        close_release.set()
        await asyncio.gather(pending, return_exceptions=True)
