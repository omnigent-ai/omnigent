"""Turn delivery must relaunch a live codex pane whose bridge was torn down.

``_delete_native_bridge_dirs`` (session delete / resource cleanup) removes a
session's native bridge dir, and the tmux pane can outlive that teardown. The
turn-time self-heal (``_ensure_native_terminal_for_turn``) used to probe only
pane liveness, so the surviving pane masked the missing bridge: the executor
found neither ``state.json`` nor a recorded startup error and failed the turn
with the generic "Codex native bridge state is missing" message. These tests
pin the third detection layer: a live codex pane whose runner-written bridge
files are all gone is closed and re-created before the turn is forwarded,
while a live pane whose bridge is intact is left alone.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.codex_native.bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    bridge_torn_down,
    prepare_bridge_dir,
    write_mcp_bridge_config,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner.native.orchestration import _codex_bridge_torn_down_for_live_pane
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


def _plant_live_codex_pane(
    registry: TerminalRegistry,
    conv_id: str,
    tmp_path: Path,
) -> list[bool]:
    """Register a live codex pane whose close calls are recorded.

    :param registry: The runner's terminal registry.
    :param conv_id: Session/conversation id the pane is keyed under.
    :param tmp_path: Temp dir for the instance's private paths.
    :returns: A list that receives one entry per ``close()`` call.
    """
    live = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "live" / "tmux.sock",
        private_dir=tmp_path / "live_private",
    )
    live.running = True
    (tmp_path / "live_private").mkdir(exist_ok=True)

    async def _alive() -> bool:
        return True

    closes: list[bool] = []

    async def _close() -> None:
        closes.append(True)

    live.is_alive = _alive  # type: ignore[method-assign]
    live.close = _close  # type: ignore[method-assign]
    with registry._lock:
        registry._by_conversation[conv_id] = {("codex", "main"): live}
        registry._instance_locks[(conv_id, "codex", "main")] = threading.Lock()
    return closes


def _build_codex_native_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_create_calls: list[str],
) -> tuple[Any, TerminalRegistry, _ScriptedHarnessClient]:
    """Build a runner app for codex-native turns with a stubbed pane launch.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param auto_create_calls: Receives the session id per stubbed launch.
    :returns: ``(app, registry, harness_client)``.
    """

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        auto_create_calls.append(session_id)
        return SessionResourceView(
            id="terminal_codex_main",
            type="terminal",
            session_id=session_id,
            name="codex",
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_codex_terminal",
        _stub_auto_create,
    )
    monkeypatch.setattr(
        "omnigent.runner.native._auto_create_codex_terminal",
        _stub_auto_create,
    )

    async def _stub_launch_codex(ctx: Any) -> SessionResourceView:
        return await _stub_auto_create(ctx.session_id, ctx.resource_registry, ctx.publish_event)

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._launch_codex",
        _stub_launch_codex,
    )
    monkeypatch.setattr("omnigent.runner.native._launch_codex", _stub_launch_codex)
    # A native turn also nudges the tool relay, which waits 30s for a bridge
    # server-info file no fake harness ever writes.
    monkeypatch.setattr(claude_native_bridge, "post_tools_changed", lambda _bridge_dir: None)

    harness_client = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    registry = TerminalRegistry()
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=registry,
    )
    return app, registry, harness_client


async def _post_codex_turn(app: Any, conv_id: str, harness_client: _ScriptedHarnessClient) -> None:
    """Deliver one codex-native user turn and wait for the harness to see it.

    :param app: The runner app.
    :param conv_id: Session/conversation id.
    :param harness_client: The scripted harness receiving the turn.
    """
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "agent",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "codex-native",
            },
        )
        assert resp.status_code == 202, resp.text
        for _ in range(500):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.01)
    assert harness_client.posted_bodies, "harness never received the turn"


@pytest.mark.asyncio
async def test_turn_relaunches_live_codex_pane_when_bridge_torn_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live pane with every bridge file gone is closed and relaunched.

    No bridge dir exists under the isolated bridge root, modeling
    ``_delete_native_bridge_dirs`` having run while the pane survived.
    """
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    conv_id = "f1e2d3c4b5a60718293a4b5c6d7e8f90"
    auto_create_calls: list[str] = []
    app, registry, harness_client = _build_codex_native_app(
        monkeypatch, auto_create_calls=auto_create_calls
    )
    closes = _plant_live_codex_pane(registry, conv_id, tmp_path)

    await _post_codex_turn(app, conv_id, harness_client)

    assert closes == [True], (
        f"the stale live pane must be closed before relaunch; close calls: {closes!r}"
    )
    assert auto_create_calls == [conv_id], (
        f"a live pane with a torn-down bridge must be relaunched before the turn; "
        f"got auto_create_calls={auto_create_calls!r}"
    )


@pytest.mark.asyncio
async def test_turn_keeps_live_codex_pane_when_bridge_intact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live pane whose launch-seeded bridge config exists is left alone.

    Only ``bridge.json`` exists (a cold boot that has not yet written
    ``state.json``), so healing here would kill a booting TUI.
    """
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    conv_id = "a2b3c4d5e6f708192a3b4c5d6e7f8091"
    write_mcp_bridge_config(prepare_bridge_dir(conv_id))
    auto_create_calls: list[str] = []
    app, registry, harness_client = _build_codex_native_app(
        monkeypatch, auto_create_calls=auto_create_calls
    )
    closes = _plant_live_codex_pane(registry, conv_id, tmp_path)

    await _post_codex_turn(app, conv_id, harness_client)

    assert closes == [], f"an intact live pane must not be closed; close calls: {closes!r}"
    assert auto_create_calls == [], (
        f"an intact live pane must not be relaunched; got {auto_create_calls!r}"
    )


def test_bridge_torn_down_requires_every_bridge_file_gone(tmp_path: Path) -> None:
    """Any single runner-written bridge file keeps the bridge un-torn."""
    bridge_dir = tmp_path / "bridge"
    assert bridge_torn_down(bridge_dir), "a missing dir has no bridge files"
    bridge_dir.mkdir()
    assert bridge_torn_down(bridge_dir), "an empty dir (resurrected content) is torn down"
    for name in ("state.json", "startup_error.json", "bridge.json"):
        marker = bridge_dir / name
        marker.write_text("{}", encoding="utf-8")
        assert not bridge_torn_down(bridge_dir), f"{name} alone must keep the bridge un-torn"
        marker.unlink()


class _LabelServerClient:
    """Server-client stub serving one labels payload, counting calls."""

    def __init__(self, labels: dict[str, str]) -> None:
        self._labels = labels
        self.calls = 0

    async def get(self, url: str, **kwargs: Any) -> Any:
        """Serve the labels endpoint payload for any GET."""
        del url, kwargs
        self.calls += 1

        labels = self._labels

        class _Response:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"labels": labels}

        return _Response()


@pytest.mark.asyncio
async def test_torn_down_check_resolves_rotated_bridge_id_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A rotated bridge id whose dir is intact is not flagged as torn down."""
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    conv_id = "b3c4d5e6f708192a3b4c5d6e7f8091a2"
    client = _LabelServerClient({CODEX_NATIVE_BRIDGE_ID_LABEL_KEY: "rotated-bridge"})

    assert await _codex_bridge_torn_down_for_live_pane(
        server_client=client,  # type: ignore[arg-type]
        session_id=conv_id,
    ), "no dir under either key means torn down"

    write_mcp_bridge_config(prepare_bridge_dir("rotated-bridge"))
    assert not await _codex_bridge_torn_down_for_live_pane(
        server_client=client,  # type: ignore[arg-type]
        session_id=conv_id,
    ), "the rotated label's intact dir must count as the executor's bridge"


@pytest.mark.asyncio
async def test_torn_down_check_skips_label_lookup_when_default_dir_intact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A healthy session-keyed bridge dir short-circuits the label fetch."""
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    conv_id = "c4d5e6f708192a3b4c5d6e7f8091a2b3"
    write_mcp_bridge_config(prepare_bridge_dir(conv_id))
    assert bridge_dir_for_bridge_id(conv_id).is_dir()
    client = _LabelServerClient({})

    torn_down = await _codex_bridge_torn_down_for_live_pane(
        server_client=client,  # type: ignore[arg-type]
        session_id=conv_id,
    )

    assert not torn_down
    assert client.calls == 0, "an intact default dir must not pay a label lookup"
