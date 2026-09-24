"""Regression guard: a stale idle-pane reap must not remove replacement Codex resources.

The native idle-pane reaper records a ``PaneRef`` that carries a conversation
id, a terminal id, a terminal name and a socket path, but no generation or
instance identity. ``_reap_native_pane`` (wired in ``create_runner_app``)
closes the terminal by id and then, in a ``finally`` block, calls
``teardown_codex_native_app_server(pane.conversation_id)`` — keyed by the
session id alone. If the pane close operation is slow, the next message /
attach installs a *replacement* Codex TUI, forwarder and app-server under the
same session id before that ``finally`` runs. The reaper then cancels the
replacement forwarder and closes the replacement app-server even though it
only ever observed the stale pane, so the fresh pane loses its backend and the
next turn fails to connect.

This test drives the **real** wired reaper closure that ``create_runner_app``
installs (``app.state.native_pane_reaper._reap``), on a pane enumerated by the
real registry wiring (``_list_native_panes``): a fake codex terminal whose
``close()`` parks after ``TerminalRegistry.close`` has already dropped its
key, a replacement terminal + app-server + forwarder installed under the same
session id while the close is parked, then the close released. It asserts the
replacement resources are left intact.

The companion terminal-exit path (``_handle_terminal_exit`` +
``_publish_terminal_exit``) is already scoped: the Codex teardown there runs
only for REQUIRED terminals (the codex TUI is auxiliary) and the terminal
eviction passes ``TerminalRegistry.close(expected=instance)``. The reaper path
has no equivalent ownership handle, so this test fails until the reaper scopes
its teardown to the instance it observed.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import (
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, RunningFlagTerminalInstance

_SESSION_ID = "5ea1ed0000000000000000000000c0de"
_TERMINAL_NAME = "codex"
_SESSION_KEY = "main"


class _LatchingCodexInstance(RunningFlagTerminalInstance):
    """Codex pane stub whose ``close()`` parks until the test releases it.

    Models the slow pane close the report names: ``TerminalRegistry.close``
    pops the registry key first and only then awaits ``instance.close()``, so
    while this latch is held the key is free for a replacement to take.
    """

    def __init__(
        self,
        *args: object,
        close_entered: asyncio.Event,
        close_gate: asyncio.Event,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._close_entered = close_entered
        self._close_gate = close_gate

    async def close(self) -> None:
        """Flip the running flag, signal entry, then park on the gate."""
        self.running = False
        self._close_entered.set()
        await self._close_gate.wait()


class _FakeAppServer:
    """Codex app-server stand-in that records which instance got closed."""

    def __init__(self, tag: str, closed: list[str]) -> None:
        self._tag = tag
        self._closed = closed

    async def close(self) -> None:
        """Record this server's tag in the shared closed-list."""
        self._closed.append(self._tag)


@pytest.mark.asyncio
async def test_stale_pane_reap_leaves_replacement_codex_resources(tmp_path: Path) -> None:
    """A reap that observed the stale pane must not tear down the replacement.

    Sequence (the report's reproduction, driven through the real wiring):

    1. A codex pane is registered and enumerated by the reaper's real
       ``_list_native_panes`` closure (role-confirmed native pane).
    2. The reap starts; ``TerminalRegistry.close`` pops the key and parks
       inside the stale instance's ``close()``.
    3. The ordinary next-message path installs a replacement terminal,
       forwarder and app-server under the same session id.
    4. The close is released; the reap's ``finally`` runs.
    5. The replacement forwarder must not be cancelled and the replacement
       app-server must not be closed.
    """
    registry = TerminalRegistry()
    resource_registry = SessionResourceRegistry(terminal_registry=registry)
    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=registry,
        resource_registry=resource_registry,
    )
    reaper = app.state.native_pane_reaper
    assert reaper is not None, "runner app did not wire the native pane reaper"

    close_entered = asyncio.Event()
    close_gate = asyncio.Event()
    stale = _LatchingCodexInstance(
        name=_TERMINAL_NAME,
        session_key=_SESSION_KEY,
        socket_path=tmp_path / "codex-main.sock",
        private_dir=tmp_path / "codex-main",
        os_env=None,
        running=True,
        close_entered=close_entered,
        close_gate=close_gate,
    )
    terminal_id = terminal_resource_id(_TERMINAL_NAME, _SESSION_KEY)
    registry._by_conversation[_SESSION_ID] = {(_TERMINAL_NAME, _SESSION_KEY): stale}
    with resource_registry._lock:
        resource_registry._terminal_roles[(_SESSION_ID, terminal_id)] = CODEX_NATIVE_TERMINAL_ROLE

    panes = [p for p in reaper._list_native_panes() if p.conversation_id == _SESSION_ID]
    assert len(panes) == 1, f"reaper wiring did not enumerate the codex pane: {panes}"
    pane = panes[0]
    assert pane.terminal_id == terminal_id

    closed_app_servers: list[str] = []
    replacement_cancelled = asyncio.Event()
    replacement_forwarder: asyncio.Task[object] | None = None
    reap_task: asyncio.Task[None] | None = None
    try:
        reap_task = asyncio.create_task(reaper._reap(pane))
        await asyncio.wait_for(close_entered.wait(), timeout=5)

        # The stale pane's key is free now (TerminalRegistry.close pops the
        # entry before awaiting instance.close()). Install the replacement
        # trio the ordinary ensure path creates for the next message.
        replacement = RunningFlagTerminalInstance(
            name=_TERMINAL_NAME,
            session_key=_SESSION_KEY,
            socket_path=tmp_path / "codex-main-2.sock",
            private_dir=tmp_path / "codex-main-2",
            os_env=None,
            running=True,
        )
        registry._by_conversation[_SESSION_ID] = {(_TERMINAL_NAME, _SESSION_KEY): replacement}

        async def _replacement_forwarder() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                replacement_cancelled.set()
                raise

        replacement_forwarder = asyncio.create_task(_replacement_forwarder())
        orchestration._register_auto_forwarder_task(_SESSION_ID, replacement_forwarder)
        orchestration._AUTO_CODEX_APP_SERVERS[_SESSION_ID] = _FakeAppServer(
            "replacement", closed_app_servers
        )
        await asyncio.sleep(0)

        # Release the parked close so the reap's ``finally`` teardown runs.
        close_gate.set()
        await asyncio.wait_for(reap_task, timeout=5)
        reap_task = None
        # Let any cancellation the teardown scheduled settle before asserting.
        await asyncio.sleep(0.2)

        assert "replacement" not in closed_app_servers, (
            "reaper of the stale pane closed the REPLACEMENT app-server "
            f"(closed={closed_app_servers}); a cleanup that observed one "
            "terminal must not remove a newer creation's resources"
        )
        assert not replacement_cancelled.is_set(), (
            "reaper of the stale pane cancelled the REPLACEMENT transcript "
            "forwarder; a cleanup that observed one terminal must not remove "
            "a newer creation's resources"
        )
        assert _SESSION_ID in orchestration._AUTO_CODEX_APP_SERVERS, (
            "reaper of the stale pane deregistered the REPLACEMENT app-server "
            "from the session slot"
        )
        assert (
            registry._by_conversation.get(_SESSION_ID, {}).get((_TERMINAL_NAME, _SESSION_KEY))
            is replacement
        ), "the replacement terminal should survive the stale reap"
    finally:
        close_gate.set()
        if reap_task is not None:
            reap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reap_task
        leftover = orchestration._AUTO_FORWARDER_TASKS.pop(_SESSION_ID, None)
        for task in {leftover, replacement_forwarder} - {None}:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        orchestration._AUTO_CODEX_APP_SERVERS.pop(_SESSION_ID, None)
        registry._by_conversation.pop(_SESSION_ID, None)
