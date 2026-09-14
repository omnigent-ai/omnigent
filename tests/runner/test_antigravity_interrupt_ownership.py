"""Ownership regressions for Antigravity native interrupt events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


def _spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "antigravity-native"}),
    )


def _label_client(conv_id: str, bridge_id: str) -> NullServerClient:
    class _LabelServerClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
            if url == f"/v1/sessions/{conv_id}/labels":

                class _Labels(self._Response):
                    def json(self) -> dict[str, Any]:
                        from omnigent.harnesses.antigravity_native.bridge import (
                            ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
                        )

                        return {"labels": {ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: bridge_id}}

                return _Labels()
            return await super().get(url, **kwargs)

    return _LabelServerClient()


class _NoLiveHarnessProcessManager(_FakeProcessManager):
    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _ScriptedHarnessClient:
        if harness == "any":
            raise NoLiveHarnessError(f"no live harness for {conversation_id}")
        return await super().get_client(conversation_id, harness, env)


def _active_tui(monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, ...]]) -> None:
    from omnigent.harnesses.antigravity_native import bridge

    monkeypatch.setattr(bridge, "_session_alive", lambda *_args: True)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_args: bridge._AGY_ACTIVE_MARKER)
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))


def _bind_transcript(bridge_dir: Path, cascade_id: str) -> None:
    from omnigent.harnesses.antigravity_native import bridge

    app_dir = bridge.agy_gemini_dir(bridge_dir) / "antigravity-cli"
    transcript_path = (
        app_dir / "brain" / cascade_id / ".system_generated" / "logs" / "transcript_full.jsonl"
    )
    transcript_path.parent.mkdir(parents=True)
    transcript_path.touch()
    cache = app_dir / "cache" / "last_conversations.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"/workspace": cascade_id}), encoding="utf-8")


async def _create_app_session(conv_id: str, bridge_id: str) -> Any:
    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _spec()

    app = create_runner_app(
        process_manager=_NoLiveHarnessProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_label_client(conv_id, bridge_id),  # type: ignore[arg-type]
    )
    return _runner_client(app)


@pytest.mark.asyncio
async def test_event_does_not_escape_or_record_after_discovery_rotates_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.antigravity_native import bridge
    from omnigent.inner import antigravity_native_executor as executor

    conv_id = "fc7a9e4c3c4141bfb96d8ad662ea28cd"
    replacement_id = "e16b291d55f94281b94fbd76ce854a7e"
    bridge_id = "shared-antigravity-bridge"
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "agy-bridges")
    client_context = await _create_app_session(conv_id, bridge_id)
    sent: list[tuple[str, ...]] = []
    recorded: list[dict[str, object]] = []
    _active_tui(monkeypatch, sent)

    def _discover(_cascade_id: str) -> None:
        bridge.write_bridge_state(
            bridge.bridge_dir_for_bridge_id(bridge_id),
            bridge.AntigravityNativeBridgeState(
                session_id=replacement_id,
                conversation_id="replacement-cascade",
            ),
        )
        return

    monkeypatch.setattr(executor, "turn_is_idle_via_tui", lambda _bridge_dir: False)
    monkeypatch.setattr(executor, "resolve_language_server_port", _discover)
    monkeypatch.setattr(
        executor,
        "record_stop_event",
        lambda _bridge_dir, payload: recorded.append(payload) or True,
    )

    async with client_context as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": conv_id, "agent_id": "agy-agent"}
        )
        assert created.status_code == 201, created.text
        bridge.write_bridge_state(
            bridge.bridge_dir_for_bridge_id(bridge_id),
            bridge.AntigravityNativeBridgeState(
                session_id=conv_id,
                conversation_id="original-cascade",
            ),
        )
        bridge.write_tmux_target(
            bridge.bridge_dir_for_bridge_id(bridge_id),
            socket_path=tmp_path / "tmux.sock",
            tmux_target="main",
        )
        response = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"})

    assert response.status_code == 204, response.text
    assert sent == []
    assert recorded == []


@pytest.mark.asyncio
async def test_event_does_not_escape_when_transcript_binding_rotates_before_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.antigravity_native import bridge
    from omnigent.inner import antigravity_native_executor as executor

    conv_id = "fc7a9e4c3c4141bfb96d8ad662ea28cd"
    bridge_id = "shared-antigravity-bridge"
    original_cascade = "c8f1b40b-03bd-4dc4-886a-dd406eeec926"
    replacement_cascade = "a6fa5475-7f93-40cb-bdc5-a9c41b269754"
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "agy-bridges")
    client_context = await _create_app_session(conv_id, bridge_id)
    sent: list[tuple[str, ...]] = []
    recorded: list[dict[str, object]] = []
    _active_tui(monkeypatch, sent)

    def _discover(_cascade_id: str) -> None:
        _bind_transcript(bridge.bridge_dir_for_bridge_id(bridge_id), replacement_cascade)
        return

    monkeypatch.setattr(executor, "turn_is_idle_via_tui", lambda _bridge_dir: False)
    monkeypatch.setattr(executor, "resolve_language_server_port", _discover)
    monkeypatch.setattr(
        executor,
        "record_stop_event",
        lambda _bridge_dir, payload: recorded.append(payload) or True,
    )

    async with client_context as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": conv_id, "agent_id": "agy-agent"}
        )
        assert created.status_code == 201, created.text
        bridge_dir = bridge.bridge_dir_for_bridge_id(bridge_id)
        bridge.write_bridge_state(
            bridge_dir,
            bridge.AntigravityNativeBridgeState(
                session_id=conv_id,
                conversation_id=original_cascade,
            ),
        )
        bridge.write_tmux_target(
            bridge_dir,
            socket_path=tmp_path / "tmux.sock",
            tmux_target="main",
        )
        response = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"})

    assert response.status_code == 503, response.text
    assert sent == []
    assert recorded == []


@pytest.mark.asyncio
async def test_event_does_not_record_replacement_after_idle_confirmation_rotates_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.antigravity_native import bridge
    from omnigent.inner import antigravity_native_executor as executor

    conv_id = "fc7a9e4c3c4141bfb96d8ad662ea28cd"
    replacement_id = "e16b291d55f94281b94fbd76ce854a7e"
    bridge_id = "shared-antigravity-bridge"
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "agy-bridges")
    client_context = await _create_app_session(conv_id, bridge_id)
    sent: list[tuple[str, ...]] = []
    recorded: list[dict[str, object]] = []
    _active_tui(monkeypatch, sent)

    def _idle(_bridge_dir: Path) -> bool:
        bridge.write_bridge_state(
            bridge.bridge_dir_for_bridge_id(bridge_id),
            bridge.AntigravityNativeBridgeState(
                session_id=replacement_id,
                conversation_id="replacement-cascade",
            ),
        )
        return True

    monkeypatch.setattr(executor, "turn_is_idle_via_tui", lambda _bridge_dir: False)
    monkeypatch.setattr(executor, "resolve_language_server_port", lambda _cascade_id: None)
    monkeypatch.setattr(executor, "wait_for_turn_idle_via_tui", _idle)
    monkeypatch.setattr(
        executor,
        "record_stop_event",
        lambda _bridge_dir, payload: recorded.append(payload) or True,
    )

    async with client_context as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": conv_id, "agent_id": "agy-agent"}
        )
        assert created.status_code == 201, created.text
        bridge_dir = bridge.bridge_dir_for_bridge_id(bridge_id)
        bridge.write_bridge_state(
            bridge_dir,
            bridge.AntigravityNativeBridgeState(
                session_id=conv_id,
                conversation_id="c8f1b40b-03bd-4dc4-886a-dd406eeec926",
            ),
        )
        _bind_transcript(bridge_dir, "c8f1b40b-03bd-4dc4-886a-dd406eeec926")
        bridge.write_tmux_target(
            bridge_dir,
            socket_path=tmp_path / "tmux.sock",
            tmux_target="main",
        )
        response = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"})

    assert response.status_code == 204, response.text
    assert sent == [(str(tmp_path / "tmux.sock"), "send-keys", "-t", "main", "Escape")]
    assert recorded == []


@pytest.mark.asyncio
async def test_event_allows_first_turn_placeholder_to_bind_same_session_cascade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.antigravity_native import bridge
    from omnigent.inner import antigravity_native_executor as executor

    conv_id = "fc7a9e4c3c4141bfb96d8ad662ea28cd"
    bridge_id = "shared-antigravity-bridge"
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "agy-bridges")
    client_context = await _create_app_session(conv_id, bridge_id)
    sent: list[tuple[str, ...]] = []
    _active_tui(monkeypatch, sent)

    def _not_idle(_bridge_dir: Path) -> bool:
        bridge.write_bridge_state(
            bridge.bridge_dir_for_bridge_id(bridge_id),
            bridge.AntigravityNativeBridgeState(
                session_id=conv_id,
                conversation_id="real-first-cascade",
            ),
        )
        return False

    monkeypatch.setattr(executor, "turn_is_idle_via_tui", _not_idle)
    monkeypatch.setattr(executor, "wait_for_turn_idle_via_tui", lambda _bridge_dir: True)

    async with client_context as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": conv_id, "agent_id": "agy-agent"}
        )
        assert created.status_code == 201, created.text
        bridge_dir = bridge.bridge_dir_for_bridge_id(bridge_id)
        bridge.write_bridge_state(
            bridge_dir,
            bridge.AntigravityNativeBridgeState(
                session_id=conv_id,
                conversation_id="agy_conv_placeholder",
            ),
        )
        bridge.write_tmux_target(
            bridge_dir,
            socket_path=tmp_path / "tmux.sock",
            tmux_target="main",
        )
        response = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"})

    assert response.status_code == 204, response.text
    assert sent == [(str(tmp_path / "tmux.sock"), "send-keys", "-t", "main", "Escape")]
