"""Regression: a session reset must fence a terminal still being launched.

The in-place agent-switch reset (``POST /reset-state``) closes the terminals
``list_for_conversation`` returns at that moment and clears the agent-derived
caches, but it does not stop a terminal creator already running.
``TerminalRegistry.launch`` starts the terminal outside the registry lock and
takes the slot only once the start completes, so without a fence at the
registry's publication point a creator that is mid-launch when the reset lands
registers its terminal *after* the reset finished — leaving the session holding
a terminal that belongs to an agent it no longer runs.

This drives ``TerminalRegistry.launch`` directly — the publication path shared
by every creator, including ``sys_terminal_launch``, which never sees the
runner app's per-context fences. Real-terminal timing is not stable enough to
hit this window (the blocking fork/tmux spawn serialises the runner loop), so
the launch is driven through a latched terminal-start stub; the reset is driven
through the real ``POST /reset-state`` endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec
from omnigent.terminals import TerminalLaunchSupersededError, TerminalRegistry
from omnigent.terminals import registry as registry_mod
from tests.runner.helpers import NullServerClient, make_test_terminal_instance


class _LatchedTerminalInstance:
    """Terminal-instance stub whose ``launch`` blocks on a latch.

    Signals ``entered`` when the launch begins (slot still empty) and completes
    only once ``release`` is set, so a reset can land in between.
    """

    def __init__(self, inner: object, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._inner = inner
        self._entered = entered
        self._release = release

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def launch(self, *, cwd: Path | None = None) -> None:
        self._entered.set()
        await self._release.wait()
        self._inner.running = True

    async def is_alive(self) -> bool:
        return bool(self._inner.running)

    async def close(self) -> None:
        self._inner.running = False


@pytest.mark.asyncio
async def test_reset_fences_a_terminal_that_is_still_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = "conv_reset_inflight"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    terminal_registry = TerminalRegistry(conversation_link_base_url="http://127.0.0.1:8000")
    resource_registry = SessionResourceRegistry(
        terminal_registry=terminal_registry,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    async def _spec_resolver(agent_id: str, session_id: str | None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="any")

    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=resource_registry,
        spec_resolver=_spec_resolver,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    created_inners: list[object] = []

    def _latched_create(*_args: object, **_kwargs: object) -> TerminalCreateResult:
        inner = make_test_terminal_instance("tui", "main", tmp_path, running=False)
        created_inners.append(inner)
        return TerminalCreateResult(
            instance=_LatchedTerminalInstance(inner, entered, release),  # type: ignore[arg-type]
            cwd=tmp_path,
        )

    monkeypatch.setattr(registry_mod, "create_terminal_instance", _latched_create)

    spec = TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )

    launch_task = asyncio.create_task(terminal_registry.launch(conv_id, "tui", "main", spec))
    await asyncio.wait_for(entered.wait(), timeout=10.0)

    # Mid-launch: the slot is still empty, so the reset sees nothing to close.
    assert terminal_registry.get(conv_id, "tui", "main") is None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        reset = await client.post(f"/v1/sessions/{conv_id}/reset-state")
        assert reset.status_code == 200, reset.text
        assert reset.json()["reset"] is True

    assert terminal_registry.get(conv_id, "tui", "main") is None
    release.set()
    # The fixed behavior: registration refused, instance discarded.
    with contextlib.suppress(TerminalLaunchSupersededError):
        await asyncio.wait_for(launch_task, timeout=10.0)

    leaked = terminal_registry.get(conv_id, "tui", "main")
    assert leaked is None, (
        "reset-state did not fence the in-flight terminal launch: the "
        "superseded creator registered its terminal after the reset "
        "completed, so the session still holds the previous agent's terminal."
    )
    assert created_inners and created_inners[0].running is False, (
        "the superseded launch's terminal instance was left running instead of closed"
    )
