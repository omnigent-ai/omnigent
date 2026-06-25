"""Unit tests for the hermes-native tmux bridge (no real tmux needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

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


# -- Compress command injection tests --


def test_inject_compress_command_sends_keys(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _fake_tmux(_socket_path, *args):
        calls.append(args)

    monkeypatch.setattr(b, "_run_tmux", _fake_tmux)
    monkeypatch.setattr(
        b, "read_tmux_info", lambda _d: {"socket_path": "/s", "tmux_target": "t"}
    )
    # _wait_for_tmux_info calls read_tmux_info internally, so patch it.
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda _d, timeout_s=30: {"socket_path": "/s", "tmux_target": "t"}
    )
    b.inject_compress_command(tmp_path)
    assert calls == [
        ("send-keys", "-t", "t", "C-u"),
        ("send-keys", "-l", "-t", "t", "/compress"),
        ("send-keys", "-t", "t", "Enter"),
    ]


def test_inject_compress_command_raises_without_target(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: None)
    with pytest.raises(RuntimeError):
        b.inject_compress_command(tmp_path, timeout_s=0.1)


# -- Policy hook config tests --


def test_write_policy_hook_config_creates_expected_files(tmp_path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    hermes_home = b.write_policy_hook_config(
        bridge_dir, "http://localhost:6767", "session-123"
    )

    assert hermes_home == bridge_dir / "hermes_home"
    assert hermes_home.is_dir()

    # Wrapper shell script exists and is executable.
    wrapper = hermes_home / "omnigent-policy-hook.sh"
    assert wrapper.is_file()
    assert wrapper.stat().st_mode & 0o111
    wrapper_text = wrapper.read_text()
    assert "_OMNIGENT_SERVER_URL='http://localhost:6767'" in wrapper_text
    assert "_OMNIGENT_SESSION_ID='session-123'" in wrapper_text
    assert sys.executable in wrapper_text
    assert "hermes_policy_hook.py" in wrapper_text

    # config.yaml with hook registered.
    config = json.loads((hermes_home / "config.yaml").read_text())
    assert config["hooks_auto_accept"] is True
    hooks = config["hooks"]["pre_tool_call"]
    assert len(hooks) == 1
    assert hooks[0]["command"] == str(wrapper)
    assert hooks[0]["timeout"] == 86400

    # Allowlist.
    allowlist = json.loads((hermes_home / "shell-hooks-allowlist.json").read_text())
    assert allowlist["approvals"][0]["event"] == "pre_tool_call"
    assert allowlist["approvals"][0]["command"] == str(wrapper)

    # MCP server registered.
    mcp = config["mcp_servers"]["omnigent"]
    assert mcp["command"] == sys.executable
    assert "serve-mcp" in mcp["args"]
    assert "--bridge-dir" in mcp["args"]
    assert str(bridge_dir) in mcp["args"]

    # bridge.json written with auth token.
    bridge_config = json.loads((bridge_dir / "bridge.json").read_text())
    assert isinstance(bridge_config["token"], str)
    assert len(bridge_config["token"]) > 0


def test_write_policy_hook_config_copies_user_files(tmp_path, monkeypatch) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    user_hermes = tmp_path / ".hermes"
    user_hermes.mkdir()
    (user_hermes / ".env").write_text("API_KEY=secret")
    (user_hermes / "auth.json").write_text('{"token": "abc"}')

    monkeypatch.setattr(b.Path, "home", staticmethod(lambda: tmp_path))

    hermes_home = b.write_policy_hook_config(bridge_dir, "http://localhost:6767", "s1")
    assert (hermes_home / ".env").read_text() == "API_KEY=secret"
    assert (hermes_home / "auth.json").read_text() == '{"token": "abc"}'


def test_write_policy_hook_config_merges_user_model(tmp_path, monkeypatch) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    user_hermes = tmp_path / ".hermes"
    user_hermes.mkdir()

    import yaml

    (user_hermes / "config.yaml").write_text(
        yaml.dump({"model": "claude-sonnet-4-20250514", "providers": {"anthropic": {}}})
    )

    monkeypatch.setattr(b.Path, "home", staticmethod(lambda: tmp_path))

    hermes_home = b.write_policy_hook_config(bridge_dir, "http://localhost:6767", "s2")
    config = json.loads((hermes_home / "config.yaml").read_text())
    assert config["model"] == "claude-sonnet-4-20250514"
    assert config["providers"] == {"anthropic": {}}
    assert config["hooks_auto_accept"] is True


def test_read_hermes_home_returns_path_when_exists(tmp_path) -> None:
    (tmp_path / "hermes_home").mkdir()
    assert b.read_hermes_home(tmp_path) == tmp_path / "hermes_home"


def test_read_hermes_home_returns_none_when_missing(tmp_path) -> None:
    assert b.read_hermes_home(tmp_path) is None


def test_build_spawn_env_includes_hermes_home_when_policy_written(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(b, "_BRIDGE_ROOT", tmp_path / "hermes-native")
    bridge_dir = b.bridge_dir_for_session_id("test-session")
    bridge_dir.mkdir(parents=True, exist_ok=True)
    b.write_policy_hook_config(bridge_dir, "http://localhost:6767", "test-session")

    env = b.build_hermes_native_spawn_env("test-session")
    assert env["HERMES_HOME"] == str(bridge_dir / "hermes_home")


def test_build_spawn_env_no_hermes_home_without_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "_BRIDGE_ROOT", tmp_path / "hermes-native")
    env = b.build_hermes_native_spawn_env("test-no-policy")
    assert "HERMES_HOME" not in env
