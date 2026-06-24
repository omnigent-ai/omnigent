"""Tests for the per-session KIMI_CODE_HOME builder."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from omnigent.kimi_native_credentials import (
    KIMI_CODE_HOME_ENV_VAR,
    build_kimi_session_home,
    render_kimi_hooks_toml,
)


def _fake_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``resolve_user_kimi_home`` at a populated fake global home."""
    user_home = tmp_path / "user-kimi"
    user_home.mkdir()
    (user_home / "config.toml").write_text(
        'default_model = "kimi-code/x"\n[providers."managed"]\ntype = "kimi"\n', encoding="utf-8"
    )
    (user_home / "oauth").mkdir()
    (user_home / "oauth" / "token").write_text("secret", encoding="utf-8")
    monkeypatch.setenv(KIMI_CODE_HOME_ENV_VAR, str(user_home))
    return user_home


def test_render_hooks_toml_is_valid_and_complete() -> None:
    toml = render_kimi_hooks_toml(bridge_dir=Path("/tmp/b r"), python_executable="/py")
    parsed = tomllib.loads(toml)
    events = {h["event"] for h in parsed["hooks"]}
    assert events == {"PreToolUse", "PermissionRequest"}
    for hook in parsed["hooks"]:
        assert "omnigent.kimi_native_hook" in hook["command"]
        assert "/tmp/b r" in hook["command"]  # space-bearing path round-trips


def test_build_session_home_preserves_user_config_and_appends_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    env = build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    assert env == {KIMI_CODE_HOME_ENV_VAR: str(session_home)}
    parsed = tomllib.loads((session_home / "config.toml").read_text(encoding="utf-8"))
    # User config preserved …
    assert parsed["default_model"] == "kimi-code/x"
    assert "managed" in parsed["providers"]
    # … and the Omnigent hooks appended.
    assert {h["event"] for h in parsed["hooks"]} == {"PreToolUse", "PermissionRequest"}


def test_build_session_home_symlinks_auth_but_not_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    # oauth is symlinked through to the user's tokens (auth keeps working) …
    oauth_link = session_home / "oauth"
    assert oauth_link.is_symlink()
    assert (oauth_link / "token").read_text(encoding="utf-8") == "secret"
    # … but config.toml is a real file (we own its content), not a symlink.
    assert not (session_home / "config.toml").is_symlink()


def test_build_session_home_without_user_home_writes_hooks_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KIMI_CODE_HOME_ENV_VAR, str(tmp_path / "does-not-exist"))
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    parsed = tomllib.loads((session_home / "config.toml").read_text(encoding="utf-8"))
    assert {h["event"] for h in parsed["hooks"]} == {"PreToolUse", "PermissionRequest"}
