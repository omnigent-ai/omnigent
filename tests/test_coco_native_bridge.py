"""Unit tests for the coco-native tmux bridge (no real tmux or cortex needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omnigent import coco_native_bridge as b


def test_bridge_dir_is_per_session_and_under_root() -> None:
    d1 = b.bridge_dir_for_session_id("conv_a")
    d2 = b.bridge_dir_for_session_id("conv_b")
    assert d1 != d2
    assert d1.parent == b.bridge_root()
    # Deterministic for the same session id.
    assert d1 == b.bridge_dir_for_session_id("conv_a")


def test_build_spawn_env_publishes_bridge_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "_BRIDGE_ROOT", tmp_path / "coco-native")
    env = b.build_coco_native_spawn_env("conv_x")
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


def test_paste_payload_bytes_lone_cr_and_unicode() -> None:
    # Lone \r normalizes like \n; non-ASCII text survives as UTF-8.
    assert b._paste_payload_bytes("x\ry\x00é") == b"x\ry\xc3\xa9"


def test_submit_needle_prefers_last_qualifying_line() -> None:
    assert b._submit_needle("hi\nthere is a longer tail line") == "there is a longer tail l"
    # Truncated to 24 chars.
    assert len(b._submit_needle("x" * 80)) == 24
    # Too-short content yields no needle (blind-submit path).
    assert b._submit_needle("ok") == ""


def test_submit_needle_falls_back_to_stripped_content() -> None:
    # No line is >= 4 chars on its own, but the stripped whole qualifies.
    assert b._submit_needle("ab\ncd") == "ab\ncd"


def _write_user_home(tmp_path, monkeypatch, *, hooks_json: str | None = None) -> Path:
    """Create a fake user ``~/.snowflake`` and point SNOWFLAKE_HOME at it."""
    user_home = tmp_path / "user_snowflake"
    (user_home / "cortex" / "conversations").mkdir(parents=True)
    (user_home / "connections.toml").write_text("[default]\n", encoding="utf-8")
    (user_home / "cortex" / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    if hooks_json is not None:
        (user_home / "cortex" / "hooks.json").write_text(hooks_json, encoding="utf-8")
    monkeypatch.setenv("SNOWFLAKE_HOME", str(user_home))
    return user_home


def test_write_coco_home_symlinks_and_hooks(tmp_path, monkeypatch) -> None:
    user_home = _write_user_home(tmp_path, monkeypatch)
    bridge_dir = tmp_path / "bridge"

    home = b.write_coco_home(bridge_dir)

    assert home == bridge_dir / "snowflake_home"
    # Top-level entries symlinked, except the cortex dir itself.
    link = home / "connections.toml"
    assert link.is_symlink() and link.resolve() == (user_home / "connections.toml").resolve()
    assert not (home / "cortex").is_symlink()
    # cortex children symlinked, except hooks.json.
    conv = home / "cortex" / "conversations"
    assert conv.is_symlink()
    assert conv.resolve() == (user_home / "cortex" / "conversations").resolve()
    assert (home / "cortex" / "config.toml").is_symlink()
    hooks_path = home / "cortex" / "hooks.json"
    assert hooks_path.is_file() and not hooks_path.is_symlink()

    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        groups = hooks[event]
        assert len(groups) == 1
        (hook,) = groups[0]["hooks"]
        assert hook["type"] == "command"
        assert "coco_native_hook.py" in hook["command"]
        assert str(bridge_dir) in hook["command"]
        assert sys.executable in hook["command"]


def test_write_coco_home_merges_wrapped_user_hooks(tmp_path, monkeypatch) -> None:
    user_group = {"hooks": [{"type": "command", "command": "echo user"}]}
    _write_user_home(
        tmp_path,
        monkeypatch,
        hooks_json=json.dumps({"hooks": {"Stop": [user_group], "PreToolUse": [user_group]}}),
    )

    home = b.write_coco_home(tmp_path / "bridge")

    hooks = json.loads((home / "cortex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    # The user's Stop group is kept, with the Omnigent relay appended after it.
    assert hooks["Stop"][0] == user_group
    assert "coco_native_hook.py" in hooks["Stop"][1]["hooks"][0]["command"]
    # Events the bridge does not register survive untouched.
    assert hooks["PreToolUse"] == [user_group]


def test_write_coco_home_merges_bare_map_user_hooks(tmp_path, monkeypatch) -> None:
    user_group = {"hooks": [{"type": "command", "command": "echo bare"}]}
    _write_user_home(tmp_path, monkeypatch, hooks_json=json.dumps({"SessionStart": [user_group]}))

    home = b.write_coco_home(tmp_path / "bridge")

    hooks = json.loads((home / "cortex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert hooks["SessionStart"][0] == user_group
    assert "coco_native_hook.py" in hooks["SessionStart"][1]["hooks"][0]["command"]


def test_write_coco_home_tolerates_malformed_user_hooks(tmp_path, monkeypatch) -> None:
    _write_user_home(tmp_path, monkeypatch, hooks_json="{not json!")

    home = b.write_coco_home(tmp_path / "bridge")

    hooks = json.loads((home / "cortex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    # Malformed user config degrades to Omnigent-only hooks, not a failure.
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}


def test_write_coco_home_is_idempotent(tmp_path, monkeypatch) -> None:
    user_home = _write_user_home(tmp_path, monkeypatch)
    bridge_dir = tmp_path / "bridge"
    home = b.write_coco_home(bridge_dir)
    # A file appearing in the user's home after the first run gets linked too.
    (user_home / "config.toml").write_text("x = 1\n", encoding="utf-8")

    assert b.write_coco_home(bridge_dir) == home

    assert (home / "config.toml").is_symlink()
    hooks = json.loads((home / "cortex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    # Re-running does not stack duplicate Omnigent hook groups.
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        assert len(hooks[event]) == 1


def test_read_coco_home(tmp_path) -> None:
    assert b.read_coco_home(tmp_path) is None
    (tmp_path / "snowflake_home").mkdir()
    assert b.read_coco_home(tmp_path) == tmp_path / "snowflake_home"


def test_coco_session_recording_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SNOWFLAKE_HOME", str(tmp_path))
    assert b.coco_session_recording_exists("") is False
    assert b.coco_session_recording_exists("sess-1") is False
    conv = tmp_path / "cortex" / "conversations"
    conv.mkdir(parents=True)
    (conv / "sess-1.history.jsonl").write_text("{}\n", encoding="utf-8")
    assert b.coco_session_recording_exists("sess-1") is True
    assert b.coco_session_recording_exists("sess-2") is False


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

    b.inject_user_message(tmp_path, content="do something now")

    # Draft cleared (C-a, C-k), buffer loaded + bracketed paste, one Enter.
    assert calls[0] == ("send-keys", "-t", "t", "C-a")
    assert calls[1] == ("send-keys", "-t", "t", "C-k")
    assert calls[2][0] == "load-buffer"
    assert calls[3][0] == "paste-buffer" and "-p" in calls[3]
    assert calls[4] == ("send-keys", "-t", "t", "Enter")
    assert len(calls) == 5
    # The temp paste file is cleaned up.
    assert not list(tmp_path.glob("paste_*.bin"))


def test_inject_user_message_requires_content(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="non-empty"):
        b.inject_user_message(tmp_path, content="")


def test_inject_user_message_raises_when_target_never_advertised(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="not advertised"):
        b.inject_user_message(tmp_path, content="hi", timeout_s=0.05)


def test_inject_user_message_dead_pane_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match="no longer running"):
        b.inject_user_message(tmp_path, content="hi")


def test_inject_interrupt_sends_escape_key_name(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        b, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(b, "_run_tmux", lambda _sock, *args: calls.append(args))
    b.inject_interrupt(tmp_path)
    # No ``-l``: Escape must be interpreted as a key name, not literal text.
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
    assert b.capture_coco_pane(tmp_path) is None  # no tmux.json
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: {"socket_path": "/s", "tmux_target": "t"})
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: False)
    assert b.capture_coco_pane(tmp_path) is None


def test_capture_pane_returns_text_when_alive(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(b, "read_tmux_info", lambda _d: {"socket_path": "/s", "tmux_target": "t"})
    monkeypatch.setattr(b, "_session_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(b, "_capture_pane", lambda *_a, **_k: "pane text")
    assert b.capture_coco_pane(tmp_path) == "pane text"


def test_write_coco_home_heals_broken_symlinks(tmp_path, monkeypatch) -> None:
    """A symlink whose target vanished since the last launch is re-created."""
    from omnigent.coco_native_bridge import write_coco_home

    user_home = tmp_path / "snowflake"
    (user_home / "cortex").mkdir(parents=True)
    (user_home / "connections.toml").write_text("[default]\n", encoding="utf-8")
    monkeypatch.setenv("SNOWFLAKE_HOME", str(user_home))
    bridge = tmp_path / "bridge"

    home = write_coco_home(bridge)
    link = home / "connections.toml"
    assert link.is_symlink() and link.exists()

    # Simulate the user's file being replaced (delete + recreate): the old
    # link target vanishes, leaving a broken symlink in the per-session home.
    (user_home / "connections.toml").unlink()
    assert link.is_symlink() and not link.exists()
    (user_home / "connections.toml").write_text("[default]\nname='new'\n", encoding="utf-8")

    write_coco_home(bridge)
    assert link.exists() and "new" in link.read_text(encoding="utf-8")


def test_write_coco_home_creates_user_conversations_dir_on_first_run(
    tmp_path, monkeypatch
) -> None:
    """First run (no ~/.snowflake yet): the user-side conversations store is
    created and symlinked, so transcripts land in the durable user home rather
    than a real dir inside the throwaway bridge dir."""
    from omnigent.coco_native_bridge import write_coco_home

    user_home = tmp_path / "fresh-snowflake"
    assert not user_home.exists()
    monkeypatch.setenv("SNOWFLAKE_HOME", str(user_home))
    bridge = tmp_path / "bridge"

    home = write_coco_home(bridge)
    assert (user_home / "cortex" / "conversations").is_dir()
    link = home / "cortex" / "conversations"
    assert link.is_symlink()
    assert link.resolve() == (user_home / "cortex" / "conversations").resolve()
