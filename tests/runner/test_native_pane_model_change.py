"""A model change for a cursor, kiro or devin session whose pane was reaped.

These harnesses switch a live pane by typing ``/model`` into it. Once the
reaper has closed the pane there is nothing to type into, and the next turn's
launch passes the session's ``model_override`` to the TUI instead. The server
saves that override before it forwards the change, so the runner answers 204
and the relaunch applies it; it used to answer 503, which the server surfaced
as a failed switch.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.terminals.native_pane_rig import (
    FakeServerClient,
    PaneRig,
    _Response,
    build_pane_rig,
)

_KEYS = pytest.mark.parametrize("key", ["cursor", "kiro", "devin"])
_PICKED = "picked-model-7"


@dataclass
class _SnapshotServer(FakeServerClient):
    """The server double, plus the launch snapshot the relaunch reads."""

    snapshot: dict[str, Any] | None = None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        response = await super().get(url, **kwargs)
        if url.endswith("/labels") or self.snapshot is None:
            return response
        body = dict(response.json())  # type: ignore[arg-type]
        body.update(self.snapshot)
        return _Response(response.status_code, body)


def _record_injections(monkeypatch: pytest.MonkeyPatch, rig: PaneRig) -> list[str]:
    """Replace the harness's ``/model`` injector; returns the models typed.

    Like the real one, it waits up to its timeout for the pane to advertise
    its tmux target, and fails when no pane does.
    """
    bridge = importlib.import_module(f"omnigent.harnesses.{rig.agent.key}_native.bridge")
    typed: list[str] = []

    def _inject(_bridge_dir: Path, *, model: str, timeout_s: float, **_kwargs: Any) -> None:
        deadline = time.monotonic() + timeout_s
        while not rig.alive():
            if time.monotonic() >= deadline:
                raise RuntimeError("no tmux target advertised")
            time.sleep(0.01)
        typed.append(model)

    monkeypatch.setattr(bridge, "inject_model_command", _inject)
    return typed


async def _model_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> tuple[PaneRig, _SnapshotServer, list[str]]:
    server = _SnapshotServer()
    rig = await build_pane_rig(tmp_path, monkeypatch, key=key, server=server)
    rig.app.state.session_harness_overrides[rig.conv_id] = rig.agent.harness
    return rig, server, _record_injections(monkeypatch, rig)


async def _post(rig: PaneRig, path: str, body: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        return await client.post(f"/v1/sessions/{rig.conv_id}{path}", json=body)


async def _change_model(rig: PaneRig, model: str) -> httpx.Response:
    return await _post(rig, "/events", {"type": "model_change", "model": model})


@_KEYS
async def test_a_model_change_after_a_reap_waits_for_the_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    rig, _server, typed = await _model_rig(tmp_path, monkeypatch, key)
    try:
        # A live pane is switched in place, as before.
        resp = await _change_model(rig, "live-model")
        assert resp.status_code == 204, resp.text
        assert typed == ["live-model"]

        assert await rig.reaper._reap(rig.pane) is True
        assert not rig.alive()

        # It used to try the missing pane and answer 503.
        resp = await _change_model(rig, _PICKED)
        assert resp.status_code == 204, resp.text
        assert typed == ["live-model"]
    finally:
        rig.drain()


@_KEYS
async def test_a_launch_in_flight_still_gets_the_model_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """A launch holding the ensure lock may have read the old override.

    So the change is still typed, into the pane that launch brings up.
    """
    rig, _server, typed = await _model_rig(tmp_path, monkeypatch, key)
    try:
        instance = rig.terminal_registry.get(rig.conv_id, rig.agent.terminal_name, "main")
        assert await rig.reaper._reap(rig.pane) is True
        locks = rig.app.state.native_terminal_ensure_locks[key]
        async with locks.setdefault(rig.conv_id, asyncio.Lock()):
            change = asyncio.create_task(_change_model(rig, _PICKED))
            await asyncio.sleep(0.05)
            # The launch brings its pane up while the injector waits for it.
            rig.terminal_registry._by_conversation.setdefault(rig.conv_id, {})[
                (rig.agent.terminal_name, "main")
            ] = instance
        resp = await change
        assert resp.status_code == 204, resp.text
        assert typed == [_PICKED]
    finally:
        rig.drain()


def _stub_launch_edges(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Stub the vendor binary lookup and the ambient auth each launch reaches for."""
    import omnigent.cli_auth as cli_auth
    import omnigent.runner._entry as runner_entry

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", lambda *_a, **_k: None)
    monkeypatch.setattr(cli_auth, "load_databricks_org_id", lambda _url: None)
    main = importlib.import_module(f"omnigent.harnesses.{key}_native.main")
    monkeypatch.setattr(main, f"resolve_{key}_executable", lambda *_a, **_k: f"/usr/bin/{key}")


class _LaunchStopped(RuntimeError):
    """Raised in place of spawning the TUI, once its argv is recorded."""


@_KEYS
async def test_the_relaunch_after_a_reap_applies_the_saved_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """The next turn's launch passes the saved ``model_override`` to the TUI.

    The server saved the pick before it forwarded the change, so the heal the
    next turn runs (the runner's ensure route) launches the TUI on it.
    """
    rig, server, typed = await _model_rig(tmp_path, monkeypatch, key)
    _stub_launch_edges(monkeypatch, key)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launched: list[list[str]] = []

    async def _launch_required_terminal(*, spec: Any, **_kwargs: Any) -> Any:
        launched.append([spec.command, *spec.args])
        raise _LaunchStopped("argv recorded")

    try:
        assert await rig.reaper._reap(rig.pane) is True
        resp = await _change_model(rig, _PICKED)
        assert resp.status_code == 204, resp.text
        # What the server's PATCH saved before forwarding the change.
        server.snapshot = {
            "workspace": str(workspace),
            "model_override": _PICKED,
            "external_session_id": "0b8f5e1c-2d7a-4c3e-9f61-5a4b3c2d1e0f",
        }
        monkeypatch.setattr(rig.resources, "launch_required_terminal", _launch_required_terminal)
        await _post(
            rig,
            "/resources/terminals",
            {
                "terminal": rig.agent.terminal_name,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        )
        assert len(launched) == 1, "the ensure route must reach the TUI launch"
        argv = launched[0]
        assert argv[argv.index("--model") + 1] == _PICKED
        assert typed == []
        assert not rig.alive()
    finally:
        rig.drain()
