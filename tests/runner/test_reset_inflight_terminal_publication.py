"""Session reset must fence an in-flight terminal publication.

``POST /v1/sessions/{id}/reset-state`` (the in-place agent-switch reset)
closes the terminals registered *at that moment* and clears the
agent-derived caches, but it does not stop a terminal creator that is
already past spec resolution. ``TerminalRegistry.launch`` starts the
terminal outside the registry lock and takes the slot only when the start
completes, so a creator that resolved the previous agent's spec before the
reset registers its terminal *after* the reset finishes — the session
keeps a terminal belonging to an agent it no longer runs.

Codex sessions leak more: creation stores the app-server in the session
slot before it registers the terminal, and ``reset_session_state`` never
calls ``teardown_codex_native_app_server``, so a reset landing in that
window leaves the app-server subprocess registered forever.

These tests drive the real runner FastAPI endpoints with a latch inside
the terminal start procedure, because the bug's window (between spawn and
registration) is not reachable deterministically with a real tmux spawn.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.terminal import TerminalCreateResult
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration as native_orchestration
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import RunningFlagTerminalInstance


class _AgentBoundServerClient:
    """AP-server stub whose session snapshot reports a mutable ``agent_id``.

    Flipping :attr:`agent_id` simulates the in-place agent switch that
    rebinds the conversation to a new agent before the runner-side reset.
    """

    class _Response:
        """Minimal 200 response carrying a fixed JSON body."""

        def __init__(self, body: dict[str, Any]) -> None:
            """:param body: JSON body returned by :meth:`json`."""
            self.status_code = 200
            self._body = body

        def json(self) -> dict[str, Any]:
            """:returns: The fixed JSON body."""
            return self._body

        def raise_for_status(self) -> None:
            """No-op: the stub always succeeds."""

    def __init__(self, workspace: str) -> None:
        """:param workspace: Absolute workspace path reported in the snapshot."""
        self.agent_id = "agent_a"
        self._workspace = workspace

    async def get(self, url: str, **kwargs: Any) -> _AgentBoundServerClient._Response:
        """Report the session snapshot with the current ``agent_id`` binding."""
        del url, kwargs
        return self._Response(
            {"created_at": 0.0, "workspace": self._workspace, "agent_id": self.agent_id}
        )

    async def post(self, url: str, **kwargs: Any) -> _AgentBoundServerClient._Response:
        """Stub POST returning an empty 200."""
        del url, kwargs
        return self._Response({})

    async def patch(self, url: str, **kwargs: Any) -> _AgentBoundServerClient._Response:
        """Stub PATCH returning an empty 200."""
        del url, kwargs
        return self._Response({})


@pytest.mark.asyncio
async def test_reset_state_fences_inflight_terminal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A creator latched mid-start must not survive a completed reset.

    Sequence (the reported window, held open with a latch):

    1. A terminal creator resolves agent A's spec and starts the terminal.
    2. The agent switch runs ``/reset-state`` to completion — the slot is
       still empty, so the reset sees nothing to close.
    3. The creator completes and registers the terminal.

    After the reset is complete, no terminal from the earlier spec
    resolution may stay registered. A fix that lets the creation complete
    and then removes the superseded resource also passes (grace window).
    """
    conv_id = "conv_reset_fence"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    spec_a = AgentSpec(
        spec_version=1,
        name="agent_a",
        os_env=OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none")),
    )
    spec_b = AgentSpec(
        spec_version=1,
        name="agent_b",
        os_env=OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none")),
    )

    async def _spec_resolver(agent_id: str, session_id: str | None) -> AgentSpec:
        """Resolve agent_a→spec_a, agent_b→spec_b (the switch target)."""
        del session_id
        return spec_a if agent_id == "agent_a" else spec_b

    terminal_registry = TerminalRegistry(conversation_link_base_url="http://127.0.0.1:8000")
    registry = SessionResourceRegistry(
        terminal_registry=terminal_registry,
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    server = _AgentBoundServerClient(str(workspace.resolve()))
    app = create_runner_app(
        server_client=server,  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=registry,
        spec_resolver=_spec_resolver,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    launch_started = asyncio.Event()
    release_launch = asyncio.Event()

    class _LatchedInstance(RunningFlagTerminalInstance):
        """Terminal whose start blocks on a latch, holding the bug's window open."""

        async def launch(self, cwd: Path | None = None) -> None:
            """Signal the latch point, then wait for the release."""
            del cwd
            launch_started.set()
            await release_launch.wait()
            self.running = True

        async def close(self) -> None:
            """Mark the instance closed without shelling out to tmux."""
            self.running = False

        def start_idle_watcher_thread(self, *args: Any, **kwargs: Any) -> None:
            """No-op: the stub has no PTY to watch."""
            del args, kwargs

    def _latched_create(
        name: str,
        session_key: str,
        spec: Any,
        *,
        parent_os_env_spec: Any = None,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        conversation_link: str | None = None,
    ) -> TerminalCreateResult:
        """Build a latched terminal instance instead of a real tmux session."""
        del spec, parent_os_env_spec, cwd_override, sandbox_override, conversation_link
        instance = _LatchedInstance(
            name=name,
            session_key=session_key,
            socket_path=tmp_path / f"{name}-{session_key}.sock",
            private_dir=tmp_path / f"{name}-{session_key}",
            os_env=None,
            running=False,
        )
        return TerminalCreateResult(instance=instance, cwd=workspace)

    monkeypatch.setattr("omnigent.terminals.registry.create_terminal_instance", _latched_create)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        creator = asyncio.create_task(
            c.post(
                f"/v1/sessions/{conv_id}/resources/terminals",
                json={"terminal": "bash", "session_key": "s1", "spec": {"command": "bash"}},
            )
        )
        try:
            await asyncio.wait_for(launch_started.wait(), timeout=10)
            # The creator resolved agent A's spec and is mid-start: nothing
            # is registered yet, so the reset sees nothing to close.
            assert terminal_registry.list_for_conversation(conv_id) == []

            # The user switches the session's agent: the server rebinds the
            # conversation and the runner-side reset runs to completion.
            server.agent_id = "agent_b"
            reset = await c.post(f"/v1/sessions/{conv_id}/reset-state")
            assert reset.status_code == 200, reset.text
            assert reset.json()["reset"] is True

            # The pre-reset creator now completes and publishes its terminal.
            release_launch.set()
            await asyncio.wait_for(creator, timeout=10)
        finally:
            release_launch.set()
            if not creator.done():
                creator.cancel()
                await asyncio.gather(creator, return_exceptions=True)

        # Grace window: a fix that removes the superseded resource shortly
        # after the creation completes is also acceptable.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if terminal_registry.get(conv_id, "bash", "s1") is None:
                break
            await asyncio.sleep(0.05)

        leaked = terminal_registry.get(conv_id, "bash", "s1")
        assert leaked is None, (
            "reset-state completed, yet the pre-reset creator's terminal "
            "(resolved from the previous agent's spec) is still registered "
            "for the session — the reset did not fence the in-flight "
            "publication."
        )

        listed = await c.get(f"/v1/sessions/{conv_id}/resources/terminals")
        assert listed.status_code == 200, listed.text
        listed_ids = [r["id"] for r in listed.json()["data"]]
        assert "terminal_bash_s1" not in listed_ids, (
            "the stale pre-reset terminal is exposed on the session's "
            f"terminal list after the reset: {listed_ids!r}"
        )


@pytest.mark.asyncio
async def test_reset_state_removes_codex_app_server_stored_before_registration(
    tmp_path: Path,
) -> None:
    """A codex app-server stored mid-creation must not survive a reset.

    Codex creation stores the app-server in the per-session slot before it
    registers the terminal (the forwarder comes later still). A reset that
    lands in that window sees no terminal to close, and
    ``reset_session_state`` never calls
    ``teardown_codex_native_app_server`` — the app-server subprocess stays
    registered for a session that no longer runs that agent.
    """
    conv_id = "conv_reset_codex_app_server"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    terminal_registry = TerminalRegistry(conversation_link_base_url="http://127.0.0.1:8000")
    registry = SessionResourceRegistry(
        terminal_registry=terminal_registry,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    async def _spec_resolver(agent_id: str, session_id: str | None) -> AgentSpec:
        """Return a minimal spec; reset-state never resolves it."""
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="any")

    app = create_runner_app(
        server_client=_AgentBoundServerClient(str(workspace.resolve())),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=registry,
        spec_resolver=_spec_resolver,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    class _FakeCodexAppServer:
        """App-server stub recording whether the reset closed it."""

        def __init__(self) -> None:
            """Start un-closed."""
            self.closed = False

        async def close(self) -> None:
            """Record the close call."""
            self.closed = True

    fake = _FakeCodexAppServer()
    # The mid-creation state: app-server stored, terminal not yet registered.
    native_orchestration._AUTO_CODEX_APP_SERVERS[conv_id] = fake  # type: ignore[assignment]
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            reset = await c.post(f"/v1/sessions/{conv_id}/reset-state")
            assert reset.status_code == 200, reset.text
            assert reset.json()["reset"] is True

        assert conv_id not in native_orchestration._AUTO_CODEX_APP_SERVERS, (
            "reset-state completed, yet the codex app-server stored before "
            "terminal registration is still in the per-session slot — the "
            "reset never tears down the app-server."
        )
        assert fake.closed, (
            "reset-state must close the codex app-server it removes; the "
            "subprocess would otherwise linger orphaned."
        )
    finally:
        native_orchestration._AUTO_CODEX_APP_SERVERS.pop(conv_id, None)
