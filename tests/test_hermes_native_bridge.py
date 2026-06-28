"""Unit tests for the hermes-native tmux bridge (no real tmux needed)."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
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


def _patch_inject_tmux(monkeypatch, calls: list[tuple[str, ...]]) -> None:
    """Common monkeypatches: live pane, instant settle, capture shows needle."""
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(b, "_settle_pane", lambda *_a, **_k: None)
    # Pane already shows the needle so the commit-wait returns immediately.
    monkeypatch.setattr(b, "_capture_pane", lambda *_a, **_k: "do something now")
    monkeypatch.setattr(b.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_run_tmux", lambda _sock, *args: calls.append(args))


def test_inject_user_message_clears_pastes_and_submits(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_inject_tmux(monkeypatch, calls)
    # No readable store → best-effort single delivery (no confirmation loop).
    monkeypatch.setattr(b, "_state_db_path", lambda _bd: None)

    b.inject_user_message(tmp_path, content="do something now")

    flat = [a[0] for a in calls]
    # Draft cleared (C-a, C-k), buffer loaded + pasted, then a single Enter.
    assert "send-keys" in flat and "load-buffer" in flat and "paste-buffer" in flat
    assert calls[0] == ("send-keys", "-t", "t", "C-a")
    assert calls[1] == ("send-keys", "-t", "t", "C-k")
    assert calls[-1] == ("send-keys", "-t", "t", "Enter")
    # The temp paste file is cleaned up.
    assert not list(tmp_path.glob("paste_*.bin"))
    # Exactly one delivery: one load-buffer, one Enter.
    assert sum(1 for a in calls if a[0] == "load-buffer") == 1
    assert sum(1 for a in calls if a[-1] == "Enter") == 1


def test_inject_user_message_single_delivery_when_store_confirms(tmp_path, monkeypatch) -> None:
    """Store row appears after the first delivery → no retry."""
    calls: list[tuple[str, ...]] = []
    _patch_inject_tmux(monkeypatch, calls)
    monkeypatch.setattr(b, "_state_db_path", lambda _bd: tmp_path / "state.db")
    # Baseline 0, then a new row (id=1) confirms delivery on the first check.
    ids = iter([0, 1])
    monkeypatch.setattr(b, "_max_message_id", lambda _p: next(ids))

    b.inject_user_message(tmp_path, content="do something now")

    assert sum(1 for a in calls if a[0] == "load-buffer") == 1, "no retry when confirmed"
    assert sum(1 for a in calls if a[-1] == "Enter") == 1


def test_inject_user_message_retries_once_when_first_not_confirmed(tmp_path, monkeypatch) -> None:
    """No new store row after delivery #1 → re-deliver once, then confirm."""
    calls: list[tuple[str, ...]] = []
    settle_calls: list[tuple] = []
    _patch_inject_tmux(monkeypatch, calls)
    monkeypatch.setattr(b, "_settle_pane", lambda *a, **k: settle_calls.append((a, k)))
    monkeypatch.setattr(b, "_DELIVERY_CONFIRM_TIMEOUT_S", 0.0)  # first confirm fails fast
    monkeypatch.setattr(b, "_state_db_path", lambda _bd: tmp_path / "state.db")
    # baseline=0 → confirm#1 sees 0 (fail) → confirm#2 sees 1 (success after retry).
    seq = iter([0, 0, 1])
    monkeypatch.setattr(b, "_max_message_id", lambda _p: next(seq))

    b.inject_user_message(tmp_path, content="do something now")

    # Two deliveries: two settles, two pastes, two Enters (one per attempt).
    assert len(settle_calls) == 2
    assert sum(1 for a in calls if a[0] == "load-buffer") == 2
    assert sum(1 for a in calls if a[-1] == "Enter") == 2


def test_inject_user_message_raises_when_never_confirmed(tmp_path, monkeypatch) -> None:
    """Store row never appears after two deliveries → raise (FIFO-safe failure)."""
    calls: list[tuple[str, ...]] = []
    _patch_inject_tmux(monkeypatch, calls)
    monkeypatch.setattr(b, "_DELIVERY_CONFIRM_TIMEOUT_S", 0.0)
    monkeypatch.setattr(b, "_state_db_path", lambda _bd: tmp_path / "state.db")
    monkeypatch.setattr(b, "_max_message_id", lambda _p: 0)  # never advances

    with pytest.raises(RuntimeError, match="did not accept"):
        b.inject_user_message(tmp_path, content="do something now")

    # Both attempts ran (two pastes) before giving up.
    assert sum(1 for a in calls if a[0] == "load-buffer") == 2


def test_inject_user_message_requires_content(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        b.inject_user_message(tmp_path, content="")


def test_state_db_path_prefers_per_session_home(tmp_path, monkeypatch) -> None:
    # No per-session home, no $HERMES_HOME, no ~/.hermes → None.
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(b.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    assert b._state_db_path(tmp_path) is None
    # Per-session HERMES_HOME under the bridge dir wins, even before state.db exists.
    home = tmp_path / b._HERMES_HOME_SUBDIR
    home.mkdir()
    assert b._state_db_path(tmp_path) == home / "state.db"


def test_max_message_id_reads_high_water_mark(tmp_path) -> None:
    db = tmp_path / "state.db"
    # Missing file → 0.
    assert b._max_message_id(db) == 0
    con = sqlite3.connect(str(db))
    con.executescript(b._MESSAGES_DDL)
    # Empty table → 0.
    assert b._max_message_id(db) == 0
    con.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
        (7, "s1", "user", "hi", 0.0),
    )
    con.commit()
    con.close()
    assert b._max_message_id(db) == 7


def test_await_new_message_checks_at_least_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b.time, "sleep", lambda *_a, **_k: None)
    # Even with a zero timeout, one check runs: baseline 5, current 6 → True.
    monkeypatch.setattr(b, "_max_message_id", lambda _p: 6)
    assert b._await_new_message(tmp_path / "state.db", 5, 0.0) is True
    # No advance → False.
    monkeypatch.setattr(b, "_max_message_id", lambda _p: 5)
    assert b._await_new_message(tmp_path / "state.db", 5, 0.0) is False


def test_inject_user_message_dead_pane_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match="no longer running"):
        b.inject_user_message(tmp_path, content="hi")


def test_inject_interrupt_sends_ctrl_c(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_run_tmux", lambda _sock, *args: calls.append(args))
    b.inject_interrupt(tmp_path)
    assert calls == [("send-keys", "-t", "t", "C-c")]


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
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: {"socket_path": "/s", "tmux_target": "t"})
    # _wait_for_tmux_info calls read_tmux_info internally, so patch it.
    monkeypatch.setattr(
        b,
        "_wait_for_tmux_info",
        lambda _d, timeout_s=30: {"socket_path": "/s", "tmux_target": "t"},
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

    hermes_home = b.write_policy_hook_config(bridge_dir, "http://localhost:6767", "session-123")

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


def test_build_spawn_env_includes_hermes_home_when_policy_written(tmp_path, monkeypatch) -> None:
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


# -- Session cloning tests --


def _create_source_db(db_path: Path, session_id: str) -> None:
    """Create a minimal Hermes state.db with a session and a few messages."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(b._SESSIONS_DDL)
    conn.execute(b._MESSAGES_DDL)
    conn.execute(
        "INSERT INTO sessions (id, source, cwd, started_at) VALUES (?, ?, ?, ?)",
        (session_id, "cli", "/old/path", 1700000000.0),
    )
    # user message
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, "user", "hello", 1700000001.0, 1),
    )
    # assistant message
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, "assistant", "hi there", 1700000002.0, 1),
    )
    # tool message with tool_calls
    conn.execute(
        "INSERT INTO messages "
        "(session_id, role, content, tool_calls, tool_name, timestamp, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, "tool", "result", '[{"id":"tc1"}]', "bash", 1700000003.0, 1),
    )
    conn.commit()
    conn.close()


def test_clone_hermes_session_copies_rows(tmp_path: Path) -> None:
    source_db = tmp_path / "source" / "state.db"
    source_db.parent.mkdir()
    target_db = tmp_path / "target" / "state.db"

    src_sid = "src-session-id"
    tgt_sid = "tgt-session-id"
    _create_source_db(source_db, src_sid)

    b.clone_hermes_session(source_db, target_db, src_sid, tgt_sid)

    # Target has the cloned session.
    tgt = sqlite3.connect(str(target_db))
    sess = tgt.execute("SELECT id, source, cwd, started_at FROM sessions").fetchall()
    assert len(sess) == 1
    assert sess[0][0] == tgt_sid
    assert sess[0][2] == "/old/path"  # cwd preserved when workspace not given

    # Target has all 3 messages with the new session_id.
    msgs = tgt.execute("SELECT session_id, role, content FROM messages ORDER BY id").fetchall()
    assert len(msgs) == 3
    assert all(m[0] == tgt_sid for m in msgs)
    assert msgs[0][1] == "user"
    assert msgs[1][1] == "assistant"
    assert msgs[2][1] == "tool"
    assert msgs[2][2] == "result"

    # tool_calls preserved on the tool message.
    tool_calls = tgt.execute("SELECT tool_calls FROM messages WHERE role = 'tool'").fetchone()[0]
    assert tool_calls == '[{"id":"tc1"}]'
    tgt.close()

    # Source is unchanged.
    src = sqlite3.connect(str(source_db))
    src_msgs = src.execute("SELECT session_id FROM messages").fetchall()
    assert all(m[0] == src_sid for m in src_msgs)
    src.close()


def test_clone_hermes_session_remaps_workspace(tmp_path: Path) -> None:
    source_db = tmp_path / "source" / "state.db"
    source_db.parent.mkdir()
    target_db = tmp_path / "target" / "state.db"

    src_sid = "src-ws"
    tgt_sid = "tgt-ws"
    _create_source_db(source_db, src_sid)

    b.clone_hermes_session(source_db, target_db, src_sid, tgt_sid, workspace="/new/path")

    tgt = sqlite3.connect(str(target_db))
    cwd = tgt.execute("SELECT cwd FROM sessions WHERE id = ?", (tgt_sid,)).fetchone()[0]
    assert cwd == "/new/path"
    tgt.close()


def test_mint_hermes_session_id_returns_uuid() -> None:
    sid = b.mint_hermes_session_id()
    # Should be a valid UUID4 string.
    parsed = uuid.UUID(sid, version=4)
    assert str(parsed) == sid
