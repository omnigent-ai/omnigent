"""Unit tests for the grok-native bridge state."""

from __future__ import annotations

from omnigent.harnesses.grok_native.bridge import (
    GrokBridgeState,
    read_bridge_state,
    write_bridge_state,
)


def test_round_trip(tmp_path) -> None:
    state = GrokBridgeState(ws_url="ws://127.0.0.1:1/ws?server-key=t", session_id="s1", pid=42)
    write_bridge_state(tmp_path, state)
    assert read_bridge_state(tmp_path) == state


def test_missing_file_returns_none(tmp_path) -> None:
    assert read_bridge_state(tmp_path) is None


def test_malformed_returns_none(tmp_path) -> None:
    (tmp_path / "bridge_state.json").write_text("{nope")
    assert read_bridge_state(tmp_path) is None


def test_missing_ws_url_returns_none(tmp_path) -> None:
    (tmp_path / "bridge_state.json").write_text('{"session_id": "s1"}')
    assert read_bridge_state(tmp_path) is None


def test_bridge_dir_env_required(monkeypatch) -> None:
    import pytest

    from omnigent.harnesses.grok_native.bridge import bridge_dir_from_env

    monkeypatch.delenv("GROK_NATIVE_BRIDGE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="GROK_NATIVE_BRIDGE_DIR"):
        bridge_dir_from_env()


def test_spawn_env_points_executor_at_session_bridge_dir() -> None:
    from omnigent.harnesses.grok_native.bridge import build_grok_native_spawn_env

    env = build_grok_native_spawn_env("sess-1")
    assert env["GROK_NATIVE_BRIDGE_DIR"].endswith("sess-1")


def test_input_ready_only_after_state_written(tmp_path, monkeypatch) -> None:
    import omnigent.harnesses.grok_native.bridge as bridge

    monkeypatch.setattr(bridge, "bridge_dir_for_session_id", lambda _sid: tmp_path)
    assert bridge.native_input_ready("sess-1", object()) is False
    write_bridge_state(tmp_path, GrokBridgeState(ws_url="ws://127.0.0.1:1/ws?server-key=t"))
    assert bridge.native_input_ready("sess-1", object()) is True
