"""Tests for the per-session KIMI_CODE_HOME builder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomllib

from omnigent import kimi_native_credentials
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
        # ``-I`` (isolated mode) is mandatory: kimi runs the hook with cwd set to
        # the session workspace, so without it a workspace containing its own
        # ``omnigent/`` shadows the install and the hook dies on ImportError
        # before publishing the approval card.
        assert " -I -m omnigent.kimi_native_hook" in hook["command"]
        # Pinned above kimi's 30s default so the permission hook survives a slow
        # web verdict (else the injected keystroke never lands); 600 is kimi's
        # ceiling.
        assert hook["timeout"] == 600


def test_build_session_home_preserves_user_config_and_appends_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    env = build_kimi_session_home(
        session_home,
        bridge_dir=bridge_dir,
        workspace=tmp_path / "workspace",
    )

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

    build_kimi_session_home(
        session_home,
        bridge_dir=bridge_dir,
        workspace=tmp_path / "workspace",
    )

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

    build_kimi_session_home(
        session_home,
        bridge_dir=bridge_dir,
        workspace=tmp_path / "workspace",
    )

    parsed = tomllib.loads((session_home / "config.toml").read_text(encoding="utf-8"))
    assert {h["event"] for h in parsed["hooks"]} == {"PreToolUse", "PermissionRequest"}


def test_build_session_home_seeds_kimi_041_workspace_trust_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    monkeypatch.setattr(kimi_native_credentials.time, "time_ns", lambda: 1788550264108000000)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    workspace = Path("/private/tmp/Fresh Trust?! Name.ABC-with-an-extra-long-tail")

    build_kimi_session_home(session_home, bridge_dir=bridge_dir, workspace=workspace)

    trust_dir = session_home / "workspace-trust"
    records = list(trust_dir.iterdir())
    assert [record.name for record in records] == [
        "wd_fresh-trust-name.abc-with-an-extra-long_6c704f283144"
    ], "workspace trust filename must match Kimi 0.41's slug and SHA-256 derivation"
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload == {
        "root": "/private/tmp/Fresh Trust?! Name.ABC-with-an-extra-long-tail",
        "trustedAt": 1788550264108,
    }
    assert isinstance(payload["root"], str)
    assert isinstance(payload["trustedAt"], int)


def test_build_session_home_workspace_trust_cannot_escape_to_global_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = _fake_user_home(tmp_path, monkeypatch)
    user_trust_dir = user_home / "workspace-trust"
    user_trust_dir.mkdir()
    existing = user_trust_dir / "wd_existing_aaaaaaaaaaaa"
    existing.write_text('{"root":"/already/trusted","trustedAt":1}', encoding="utf-8")
    global_before = {path.name: path.read_bytes() for path in user_trust_dir.iterdir()}

    session_home = tmp_path / "session-home"
    session_home.mkdir()
    (session_home / "workspace-trust").symlink_to(user_trust_dir, target_is_directory=True)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(
        session_home,
        bridge_dir=bridge_dir,
        workspace=tmp_path / "never-globally-trusted",
    )

    global_after = {path.name: path.read_bytes() for path in user_trust_dir.iterdir()}
    assert global_after == global_before, "workspace trust write escaped into the global Kimi home"
    session_trust_dir = session_home / "workspace-trust"
    assert not session_trust_dir.is_symlink()
    assert not (session_trust_dir / existing.name).exists()
    assert len(list(session_trust_dir.iterdir())) == 1
