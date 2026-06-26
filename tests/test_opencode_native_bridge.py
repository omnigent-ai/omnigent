"""Tests for the native OpenCode bridge state helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent import opencode_native_bridge as bridge
from omnigent.opencode_native_bridge import (
    OpenCodeNativeBridgeState,
    auth_headers_for_secret,
    bridge_dir_for_bridge_id,
    build_opencode_native_spawn_env,
    clear_bridge_state,
    ensure_auth_secret,
    prepare_bridge_dir,
    read_bridge_state,
    update_active_message_id,
    update_last_event_id,
    update_model_override,
    write_bridge_state,
    write_cost_popup_config,
    write_opencode_policy_plugin,
    write_relay_bridge_config,
    xdg_config_home_for_bridge_dir,
    xdg_data_home_for_bridge_dir,
)


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated bridge directory rooted under a tmp path."""
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    return prepare_bridge_dir("bridge_test")


def _state(bridge_dir: Path, **overrides: object) -> OpenCodeNativeBridgeState:
    base = {
        "session_id": "conv_abc",
        "server_base_url": "http://127.0.0.1:49231",
        "opencode_session_id": "ses_abc",
        "auth_secret": "s3cret",
        "xdg_data_home": str(xdg_data_home_for_bridge_dir(bridge_dir)),
        "xdg_config_home": str(xdg_config_home_for_bridge_dir(bridge_dir)),
    }
    base.update(overrides)
    return OpenCodeNativeBridgeState(**base)  # type: ignore[arg-type]


def test_prepare_bridge_dir_creates_xdg_roots(bridge_dir: Path) -> None:
    assert bridge_dir.is_dir()
    assert xdg_data_home_for_bridge_dir(bridge_dir).is_dir()
    assert xdg_config_home_for_bridge_dir(bridge_dir).is_dir()
    # 0700 perms on the bridge dir.
    assert (os.stat(bridge_dir).st_mode & 0o777) == 0o700


def test_write_read_state_round_trips(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir, model_override="anthropic/claude-opus-4"))
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.session_id == "conv_abc"
    assert loaded.opencode_session_id == "ses_abc"
    assert loaded.server_base_url == "http://127.0.0.1:49231"
    assert loaded.auth_secret == "s3cret"
    assert loaded.model_override == "anthropic/claude-opus-4"
    assert loaded.status == "idle"


def test_read_missing_state_is_none(bridge_dir: Path) -> None:
    assert read_bridge_state(bridge_dir) is None


def test_read_corrupt_state_is_none(bridge_dir: Path) -> None:
    (bridge_dir / "state.json").write_text("{not json", encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None


def test_read_incomplete_state_is_none(bridge_dir: Path) -> None:
    (bridge_dir / "state.json").write_text(json.dumps({"session_id": "x"}), encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None


def test_clear_state_removes_file(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir))
    clear_bridge_state(bridge_dir)
    assert read_bridge_state(bridge_dir) is None
    # Idempotent.
    clear_bridge_state(bridge_dir)


def test_update_active_message_id(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir))
    update_active_message_id(bridge_dir, "msg_1", status="busy")
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.active_message_id == "msg_1"
    assert loaded.status == "busy"
    update_active_message_id(bridge_dir, None, status="idle")
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.active_message_id is None
    assert loaded.status == "idle"


def test_update_model_override(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir))
    assert update_model_override(bridge_dir, "anthropic/claude-opus-4") is True
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.model_override == "anthropic/claude-opus-4"
    # Blank clears the override.
    assert update_model_override(bridge_dir, "  ") is True
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.model_override is None


def test_update_model_override_no_state_returns_false(bridge_dir: Path) -> None:
    # No bridge state written yet (server not launched).
    assert update_model_override(bridge_dir, "x/y") is False


def test_write_relay_bridge_config_writes_token_and_is_idempotent(bridge_dir: Path) -> None:
    write_relay_bridge_config(bridge_dir)
    config_path = bridge_dir / "bridge.json"
    assert config_path.exists()
    payload = json.loads(config_path.read_text())
    token = payload["token"]
    assert isinstance(token, str) and token
    # Idempotent: a second call must NOT rotate the token (the relay HTTP server
    # may already have been started with it).
    write_relay_bridge_config(bridge_dir)
    assert json.loads(config_path.read_text())["token"] == token


def test_write_cost_popup_config_writes_ap_routing(bridge_dir: Path) -> None:
    path = write_cost_popup_config(
        bridge_dir,
        ap_server_url="http://127.0.0.1:6767",
        ap_auth_headers={"Authorization": "Bearer tok"},
    )
    payload = json.loads(path.read_text())
    assert payload == {
        "ap_server_url": "http://127.0.0.1:6767",
        "ap_auth_headers": {"Authorization": "Bearer tok"},
    }
    # Rewritten (not skipped) so a later checkpoint gets a fresh token.
    write_cost_popup_config(bridge_dir, ap_server_url="http://h:1", ap_auth_headers={})
    assert json.loads(path.read_text()) == {"ap_server_url": "http://h:1", "ap_auth_headers": {}}


def test_write_opencode_policy_plugin(bridge_dir: Path) -> None:
    path = write_opencode_policy_plugin(bridge_dir)
    assert path.name == "omnigent-policy.js"
    src = path.read_text(encoding="utf-8")
    # The two phase hooks the reactive permission path can't reach.
    assert '"chat.message"' in src  # REQUEST phase
    assert '"tool.execute.after"' in src  # TOOL_RESULT phase
    # Posts the proto phases + reads its coordinates from env.
    assert "PHASE_REQUEST" in src and "PHASE_TOOL_RESULT" in src
    assert "OMNIGENT_POLICY_URL" in src and "OMNIGENT_SESSION_ID" in src
    assert "/policies/evaluate" in src
    # A function export so opencode's Object.values(mod) loader picks it up.
    assert "export const OmnigentPolicyPlugin" in src
    # Idempotent overwrite (re-launch ships fresh code, no error).
    assert write_opencode_policy_plugin(bridge_dir) == path


def test_update_last_event_id(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir))
    update_last_event_id(bridge_dir, "evt_42")
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_event_id == "evt_42"


def test_ensure_auth_secret_is_stable_and_0600(bridge_dir: Path) -> None:
    secret = ensure_auth_secret(bridge_dir)
    assert secret
    # Same secret on a second call (reused across server restarts).
    assert ensure_auth_secret(bridge_dir) == secret
    path = bridge_dir / "auth.secret"
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_auth_headers_for_secret() -> None:
    assert auth_headers_for_secret(None) == {}
    headers = auth_headers_for_secret("pw")
    assert headers["Authorization"].startswith("Basic ")
    import base64

    decoded = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "opencode:pw"


def test_state_auth_headers_method(bridge_dir: Path) -> None:
    state = _state(bridge_dir, auth_secret="pw")
    assert state.auth_headers()["Authorization"].startswith("Basic ")


def test_spawn_env_points_at_bridge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    env = build_opencode_native_spawn_env("conv_abc")
    assert env["HARNESS_OPENCODE_NATIVE_BRIDGE_DIR"] == str(bridge_dir_for_bridge_id("conv_abc"))
    assert env["HARNESS_OPENCODE_NATIVE_REQUEST_SESSION_ID"] == "conv_abc"


def test_spawn_env_bridge_id_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    env = build_opencode_native_spawn_env("conv_abc", bridge_id="bridge_xyz")
    assert env["HARNESS_OPENCODE_NATIVE_BRIDGE_DIR"] == str(bridge_dir_for_bridge_id("bridge_xyz"))
    assert env["HARNESS_OPENCODE_NATIVE_REQUEST_SESSION_ID"] == "conv_abc"


def test_seed_opencode_auth_copies_user_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The user's auth.json is copied into the per-session XDG_DATA_HOME (0600)."""
    user_data = tmp_path / "user-share"
    (user_data / "opencode").mkdir(parents=True)
    (user_data / "opencode" / "auth.json").write_text('{"anthropic": {"type": "api"}}')
    monkeypatch.setenv("XDG_DATA_HOME", str(user_data))

    bridge_dir = bridge.prepare_bridge_dir("conv_seed")
    dest = bridge.seed_opencode_auth(bridge_dir)
    assert dest is not None and dest.is_file()
    assert dest == bridge.xdg_data_home_for_bridge_dir(bridge_dir) / "opencode" / "auth.json"
    assert "anthropic" in dest.read_text()
    assert (os.stat(dest).st_mode & 0o777) == 0o600


def test_seed_opencode_auth_noop_without_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No user auth.json → no-op (returns None), e.g. on a remote runner."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-share"))
    bridge_dir = bridge.prepare_bridge_dir("conv_noseed")
    assert bridge.seed_opencode_auth(bridge_dir) is None
