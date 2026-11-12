"""Unit tests for the cline-native harness modules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.cline_native.bridge import (
    ClineBridgeState,
    build_cline_native_spawn_env,
    clear_bridge_state,
    native_input_ready,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.harnesses.cline_native.items import MirroredItem, hub_event_to_items
from omnigent.harnesses.cline_native.serve import parse_hub_start_json


def _event(name: str, payload: dict | None = None, session_id: str = "s1") -> dict:
    return {
        "version": "v1",
        "event": name,
        "sessionId": session_id,
        "payload": payload or {},
    }


class TestBridgeState:
    def test_round_trip(self, tmp_path: Path) -> None:
        state = ClineBridgeState(
            ws_url="ws://127.0.0.1:9999/hub",
            auth_token="tok",
            session_id="hub-sess-1",
            pid=123,
            server_url="http://127.0.0.1:6767",
            headers={"Authorization": "Bearer x"},
        )
        write_bridge_state(tmp_path, state)
        assert read_bridge_state(tmp_path) == state

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert read_bridge_state(tmp_path) is None
        (tmp_path / "bridge_state.json").write_text("{not json")
        assert read_bridge_state(tmp_path) is None
        (tmp_path / "bridge_state.json").write_text('{"ws_url": ""}')
        assert read_bridge_state(tmp_path) is None

    def test_clear(self, tmp_path: Path) -> None:
        write_bridge_state(tmp_path, ClineBridgeState(ws_url="ws://x"))
        clear_bridge_state(tmp_path)
        assert read_bridge_state(tmp_path) is None
        clear_bridge_state(tmp_path)  # idempotent

    def test_input_ready_requires_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "omnigent.harnesses.cline_native.bridge.bridge_dir_for_session_id",
            lambda _sid: tmp_path,
        )
        assert native_input_ready("sess", object()) is False
        write_bridge_state(tmp_path, ClineBridgeState(ws_url="ws://x"))
        assert native_input_ready("sess", object()) is False
        write_bridge_state(tmp_path, ClineBridgeState(ws_url="ws://x", session_id="hub-1"))
        assert native_input_ready("sess", object()) is True

    def test_spawn_env_keys_bridge_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "omnigent.harnesses.cline_native.bridge.bridge_dir_for_session_id",
            lambda sid: tmp_path / sid,
        )
        env = build_cline_native_spawn_env("abc")
        assert env == {"CLINE_NATIVE_BRIDGE_DIR": str(tmp_path / "abc")}


class TestHubStartParse:
    def test_parse(self) -> None:
        raw = json.dumps(
            {
                "url": "ws://127.0.0.1:25463/hub",
                "authToken": "abc123",
                "cwd": "/tmp",
                "platform": "linux",
            }
        )
        ep = parse_hub_start_json(raw)
        assert ep.ws_url == "ws://127.0.0.1:25463/hub"
        assert ep.auth_token == "abc123"

    def test_parse_missing_fields(self) -> None:
        from omnigent.harnesses.cline_native.serve import ClineHubStartError

        with pytest.raises(ClineHubStartError):
            parse_hub_start_json('{"url": "ws://x"}')
        with pytest.raises(ClineHubStartError):
            parse_hub_start_json("{}")


class TestItems:
    def test_assistant_finished_maps_message(self) -> None:
        items = hub_event_to_items(_event("assistant.finished", {"text": "hello"}))
        assert items == [MirroredItem(kind="message", role="assistant", text="hello")]

    def test_reasoning_finished_maps_reasoning(self) -> None:
        items = hub_event_to_items(_event("reasoning.finished", {"reasoning": "thinking"}))
        assert items == [MirroredItem(kind="reasoning", role="assistant", text="thinking")]

    def test_tool_started_maps_function_call(self) -> None:
        items = hub_event_to_items(
            _event(
                "tool.started",
                {"toolCallId": "c1", "toolName": "bash", "input": {"cmd": "ls"}},
            )
        )
        assert len(items) == 1
        item = items[0]
        assert item.kind == "function_call"
        assert item.call_id == "c1"
        assert item.name == "bash"
        assert json.loads(item.arguments) == {"cmd": "ls"}

    def test_tool_finished_maps_output(self) -> None:
        items = hub_event_to_items(
            _event(
                "tool.finished",
                {"toolCallId": "c1", "toolName": "bash", "output": "done"},
            )
        )
        assert items == [MirroredItem(kind="function_call_output", call_id="c1", text="done")]

    def test_tool_finished_error_maps_output(self) -> None:
        items = hub_event_to_items(
            _event(
                "tool.finished",
                {"toolCallId": "c1", "error": "boom"},
            )
        )
        assert items == [MirroredItem(kind="function_call_output", call_id="c1", text="boom")]

    def test_unmapped_events_yield_nothing(self) -> None:
        for name in (
            "run.started",
            "run.completed",
            "usage.updated",
            "assistant.delta",
            "approval.requested",
            "session.created",
        ):
            assert hub_event_to_items(_event(name, {"text": "x"})) == []

    def test_empty_text_yields_nothing(self) -> None:
        assert hub_event_to_items(_event("assistant.finished", {"text": ""})) == []
        assert hub_event_to_items(_event("tool.finished", {"toolCallId": "c"})) == []


class TestExecutorHelpers:
    def test_latest_user_text(self) -> None:
        from omnigent.inner.cline_native_executor import _latest_user_text

        assert _latest_user_text([]) == ""
        assert (
            _latest_user_text(
                [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "mid"},
                    {"role": "user", "content": "latest"},
                ]
            )
            == "latest"
        )
        assert (
            _latest_user_text(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "a"},
                            {"type": "input_text", "text": "b"},
                        ],
                    }
                ]
            )
            == "a\n\nb"
        )
