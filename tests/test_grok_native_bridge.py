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
