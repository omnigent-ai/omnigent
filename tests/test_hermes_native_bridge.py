"""Unit tests for the hermes-native tmux bridge (no real tmux needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent import hermes_native_bridge as b


def test_bridge_dir_is_per_session_and_under_root() -> None:
    d1 = b.bridge_dir_for_session_id("conv_a")
    d2 = b.bridge_dir_for_session_id("conv_b")
    assert d1 != d2
    assert d1.parent == b.bridge_root()
    # Deterministic for the same session id.
    assert d1 == b.bridge_dir_for_session_id("conv_a")


def test_build_spawn_env_publishes_bridge_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "_BRIDGE_ROOT", tmp_path / "hermes-native")
    env = b.build_hermes_native_spawn_env("conv_x")
    assert env[b.BRIDGE_DIR_ENV_VAR] == str(b.bridge_dir_for_session_id("conv_x"))
    # The dir is created so the executor can read the advertised target.
    assert Path(env[b.BRIDGE_DIR_ENV_VAR]).is_dir()


def test_write_then_read_tmux_target_roundtrip(tmp_path) -> None:
    b.write_tmux_target(tmp_path, socket_path=Path("/tmp/sock"), tmux_target="sess:0.0", pid=42)
    info = b.read_tmux_info(tmp_path)
    assert info == {"socket_path": "/tmp/sock", "tmux_target": "sess:0.0"}


def test_read_tmux_info_missing_and_malformed(tmp_path) -> None:
    assert b.read_tmux_info(tmp_path) is None  # no tmux.json
    (tmp_path / "tmux.json").write_text("not json", encoding="utf-8")
    assert b.read_tmux_info(tmp_path) is None
    (tmp_path / "tmux.json").write_text(json.dumps({"socket_path": ""}), encoding="utf-8")
    assert b.read_tmux_info(tmp_path) is None  # incomplete


def test_paste_payload_bytes_normalizes() -> None:
    out = b._paste_payload_bytes("a\r\nb\tc\x1b\n")
    # \r\n and \n → CR (0x0D); tab kept; ESC (control) dropped.
    assert out == b"a\rb\tc\r"


def test_submit_needle_prefers_last_qualifying_line() -> None:
    assert b._submit_needle("hi\nthere is a longer tail line") == "there is a longer tail l"[:24]
    # Too-short content yields no needle (blind-submit path).
    assert b._submit_needle("ok") == ""


def test_inject_user_message_clears_pastes_and_submits(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(b, "_settle_pane", lambda *_a, **_k: None)
    # Pane already shows the needle so the commit-wait returns immediately.
    monkeypatch.setattr(b, "_capture_pane", lambda *_a, **_k: "do something now")
    monkeypatch.setattr(b.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_run_tmux", lambda _sock, *args: calls.append(args))

    b.inject_user_message(tmp_path, content="do something now")

    flat = [a[0] for a in calls]
    # Draft cleared (C-a, C-k), buffer loaded + pasted, then a single Enter.
    assert "send-keys" in flat and "load-buffer" in flat and "paste-buffer" in flat
    assert calls[0] == ("send-keys", "-t", "t", "C-a")
    assert calls[1] == ("send-keys", "-t", "t", "C-k")
    assert calls[-1] == ("send-keys", "-t", "t", "Enter")
    # The temp paste file is cleaned up.
    assert not list(tmp_path.glob("paste_*.bin"))


def test_inject_user_message_requires_content(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        b.inject_user_message(tmp_path, content="")


def test_inject_user_message_dead_pane_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match="no longer running"):
        b.inject_user_message(tmp_path, content="hi")


def test_inject_interrupt_sends_escape(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_run_tmux", lambda _sock, *args: calls.append(args))
    b.inject_interrupt(tmp_path)
    assert calls == [("send-keys", "-t", "t", "Escape")]


def test_kill_session_kills_target(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_run_tmux", lambda _sock, *args: calls.append(args))
    b.kill_session(tmp_path)
    assert calls == [("kill-session", "-t", "t")]


def test_capture_pane_none_when_no_target_or_dead(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: None)
    assert b.capture_hermes_pane(tmp_path) is None
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: {"socket_path": "/s", "tmux_target": "t"})
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: False)
    assert b.capture_hermes_pane(tmp_path) is None


def test_capture_pane_returns_text_when_alive(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: {"socket_path": "/s", "tmux_target": "t"})
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(b, "_capture_pane", lambda *_a, **_k: "pane text")
    assert b.capture_hermes_pane(tmp_path) == "pane text"


def test_send_pane_keys_forwards_to_tmux(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: {"socket_path": "/s", "tmux_target": "t"})
    monkeypatch.setattr(b, "_run_tmux", lambda _sock, *args: calls.append(args))
    b.send_hermes_pane_keys(tmp_path, "4")
    assert calls == [("send-keys", "-t", "t", "4")]


def test_send_pane_keys_raises_without_target(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: None)
    with pytest.raises(RuntimeError, match="not advertised"):
        b.send_hermes_pane_keys(tmp_path, "1")
