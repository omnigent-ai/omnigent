"""Native-pane reaper leak: finished sessions judged busy forever.

A finished native session must read *idle* to the native-pane reaper so its
pane and helper processes are reclaimed after the idle window. On buggy main
the reaper's busy check short-circuits on a stale ``_native_pane_status ==
"running"`` that no turn-end channel ever clears for Codex/Antigravity, so the
pane is judged busy forever and the host leaks processes until it stalls.

These reproduce the reporter's own method: drive a real turn through the
runner's HTTP routes, end it the way the harness does in production (Codex and
Antigravity report idle only via the relayed ``external_session_status`` event),
then ask the reaper whether the quiet, unattended pane is still busy.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native import native_cost_popup
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals.pane_reaper import (
    NATIVE_PANE_TERMINAL_NAMES,
    NativePaneReaper,
    PaneRef,
)
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _NativeBlockingHarnessClient,
    _runner_client,
)
from tests.runner.helpers import NullServerClient

_CONV = "8" * 32
_AGENT = "a" * 32


def _build_reaper_app(harness: str, gate: asyncio.Event):
    """Runner app that resolves to *harness* and owns a live pane reaper.

    Combines the reaper wiring (a real terminal + resource registry, as the
    reaper is only constructed when the resource registry exposes
    ``native_panes``) with a native turn-driving harness fake.
    """
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    registry = TerminalRegistry()
    pm = _FakeProcessManager(_NativeBlockingHarnessClient(gate))
    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        terminal_registry=registry,
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


async def _drive_finished_turn(
    client: httpx.AsyncClient, harness: str, gate: asyncio.Event
) -> int:
    """Create a session and run one message turn to completion via HTTP.

    Returns the turn event POST's status code so the caller can confirm the
    turn ran instead of silently erroring behind the gate.
    """
    r = await client.post("/v1/sessions", json={"session_id": _CONV, "agent_id": _AGENT})
    assert r.status_code == 201, r.text

    turn: dict[str, httpx.Response] = {}

    async def _turn() -> None:
        turn["response"] = await client.post(
            f"/v1/sessions/{_CONV}/events",
            json={
                "type": "message",
                "role": "user",
                "model": harness,
                "harness": harness,
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )

    task = asyncio.create_task(_turn())
    await asyncio.sleep(0.3)
    gate.set()
    await asyncio.wait_for(task, timeout=10)
    return turn["response"].status_code


async def _wait_for_busy(
    reaper: NativePaneReaper, pane: PaneRef, expected: bool, *, timeout: float = 5.0
) -> None:
    # The runner updates _native_pane_status in the background after the event
    # POST returns, so poll the reaper's verdict to a deadline instead of
    # racing it with a fixed sleep that flakes on a loaded host.
    deadline = asyncio.get_running_loop().time() + timeout
    while await reaper._is_busy(pane) != expected:
        assert asyncio.get_running_loop().time() < deadline, (
            f"reaper busy verdict never became {expected}"
        )
        await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("harness", "pane_name"),
    [("codex-native", "codex"), ("antigravity-native", "antigravity")],
)
async def test_finished_native_pane_reads_idle(
    harness: str, pane_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reaper binds these by name at app build time, so stub first (no tmux).
    # monkeypatch restores the globals so the stub can't leak into later tests;
    # with the pane quiet and unattended, only a stale status could read "busy".
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda *_a, **_k: [])
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", lambda *_a, **_k: None)

    gate = asyncio.Event()
    app = _build_reaper_app(harness, gate)
    reaper = app.state.native_pane_reaper
    assert reaper is not None

    pane = PaneRef(
        _CONV,
        terminal_resource_id(pane_name, "main"),
        pane_name,
        Path("/tmp/omni-reaper-test.sock"),
    )

    async with _runner_client(app) as client:
        assert await _drive_finished_turn(client, harness, gate) == 202

        # Before any idle is relayed the finished pane must still read busy:
        # codex/antigravity emit no local turn-end edge, so a passing final
        # assertion only means something if the pane was busy to begin with.
        await _wait_for_busy(reaper, pane, True)

        # The turn is over; the harness reports idle only through the relayed
        # external_session_status event the server posts back to the runner.
        r = await client.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "external_session_status", "data": {"status": "idle"}},
        )
        assert r.status_code == 204, r.text

        # A finished, quiet, unattended pane that has reported idle must NOT be
        # judged busy — otherwise its idle timer never fires and it leaks.
        await _wait_for_busy(reaper, pane, False)


def test_devin_native_panes_are_reapable() -> None:
    # devin-native opens a TUI pane, so a missing name here leaks every
    # finished Devin session; kimi is intentionally exempt.
    assert "devin" in NATIVE_PANE_TERMINAL_NAMES
    assert "kimi" not in NATIVE_PANE_TERMINAL_NAMES
