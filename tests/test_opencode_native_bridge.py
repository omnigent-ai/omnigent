"""Tests for the native OpenCode bridge state helpers."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.opencode_native import bridge
from omnigent.harnesses.opencode_native.bridge import (
    OpenCodeNativeBridgeState,
    auth_headers_for_secret,
    bridge_dir_for_bridge_id,
    build_opencode_native_spawn_env,
    clear_bridge_state,
    copy_opencode_database_for_fork,
    ensure_auth_secret,
    opencode_db_path_for_bridge_dir,
    prepare_bridge_dir,
    read_bridge_state,
    update_active_message_id,
    update_last_applied_model,
    update_last_event_id,
    update_model_override,
    user_opencode_config_path,
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


def test_write_opencode_policy_plugin_is_v2_package(bridge_dir: Path) -> None:
    path = write_opencode_policy_plugin(bridge_dir)
    # opencode 2.x only loads configured local plugins that are directories.
    assert path == bridge_dir / "omnigent-policy"
    assert path.is_dir()
    package = json.loads((path / "package.json").read_text(encoding="utf-8"))
    assert package["type"] == "module"
    src = (path / "server.js").read_text(encoding="utf-8")
    assert "export default" in src and 'id: "omnigent-policy"' in src
    assert 'ctx.session.hook("prompt"' in src
    assert 'ctx.tool.hook("execute.after"' in src
    assert "PHASE_REQUEST" in src and "PHASE_TOOL_RESULT" in src
    assert "OMNIGENT_POLICY_URL" in src and "OMNIGENT_SESSION_ID" in src
    assert "OMNIGENT_POLICY_HEADERS" in src and "...POLICY_HEADERS" in src
    assert "/policies/evaluate" in src
    # No v1 shapes and no unresolved package import from a bare bridge dir.
    for v1 in (
        '"chat.message"',
        "export const OmnigentPolicyPlugin",
        "require(",
        "@opencode/plugin",
    ):
        assert v1 not in src
    # Idempotent overwrite.
    assert write_opencode_policy_plugin(bridge_dir) == path


_PLUGIN_HARNESS = r"""
import path from "node:path"
import { pathToFileURL } from "node:url"
const [, , pluginDir, verdictJson, mode] = process.argv
const calls = []
globalThis.fetch = async (url, init) => {
  calls.push({ url, body: JSON.parse(init.body) })
  if (mode === "throw") throw new Error("connection refused")
  return { ok: true, json: async () => JSON.parse(verdictJson) }
}
const modUrl = pathToFileURL(path.join(pluginDir, "server.js")).href
const mod = await import(modUrl)
const hooks = {}
const register = (domain) => async (name, cb) => {
  hooks[domain + "." + name] = cb
  return { dispose: async () => {} }
}
await mod.default.setup({
  session: { hook: register("session") },
  tool: { hook: register("tool") },
})
const out = { id: mod.default.id, hooks: Object.keys(hooks).sort() }
try {
  await hooks["session.prompt"]({
    sessionID: "s",
    messageID: "m",
    prompt: { text: "hi" },
    delivery: "steer",
  })
  out.prompt = "allowed"
} catch (e) {
  out.prompt = "blocked: " + e.message
}
const ev = {
  tool: "shell",
  sessionID: "s",
  status: "completed",
  result: { content: "secret", output: { x: 1 } },
}
await hooks["tool.execute.after"](ev)
out.result = ev.result
out.calls = calls
console.log(JSON.stringify(out))
"""


def _run_plugin(tmp_path: Path, plugin_dir: Path, verdict: dict, mode: str) -> dict:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.mjs"
    harness.write_text(_PLUGIN_HARNESS, encoding="utf-8")
    env = {
        **os.environ,
        "OMNIGENT_POLICY_URL": "http://srv/",
        "OMNIGENT_SESSION_ID": "conv_1",
        "OMNIGENT_RELAY_FILE": "",
    }
    proc = subprocess.run(
        [node, str(harness), str(plugin_dir), json.dumps(verdict), mode],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout)


def test_policy_plugin_denies_prompt_and_withholds_tool_result(
    bridge_dir: Path, tmp_path: Path
) -> None:
    out = _run_plugin(
        tmp_path,
        write_opencode_policy_plugin(bridge_dir),
        {"result": "POLICY_ACTION_DENY", "reason": "nope"},
        "ok",
    )
    assert out["id"] == "omnigent-policy"
    assert out["hooks"] == ["session.prompt", "tool.execute.after"]
    assert out["prompt"] == "blocked: Omnigent policy blocked this prompt: nope"
    assert out["result"] == {"content": "[Omnigent policy withheld this tool result: nope]"}
    assert out["calls"][0] == {
        "url": "http://srv/v1/sessions/conv_1/policies/evaluate",
        "body": {"event": {"type": "PHASE_REQUEST", "target": "", "data": {"text": "hi"}}},
    }
    assert out["calls"][1]["body"]["event"] == {
        "type": "PHASE_TOOL_RESULT",
        "target": "shell",
        "data": {"result": "secret"},
    }


def test_policy_plugin_fails_open_on_transport_error(bridge_dir: Path, tmp_path: Path) -> None:
    out = _run_plugin(tmp_path, write_opencode_policy_plugin(bridge_dir), {}, "throw")
    assert out["prompt"] == "allowed"
    assert out["result"] == {"content": "secret", "output": {"x": 1}}


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


def _user_share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    share = tmp_path / "user-share"
    (share / "opencode").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(share))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    return share


def test_seed_opencode_auth_copies_user_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    share = _user_share(monkeypatch, tmp_path)
    (share / "opencode" / "auth.json").write_text('{"anthropic": {"type": "api", "key": "k"}}')
    bridge_dir = bridge.prepare_bridge_dir("conv_seed")
    dest = bridge.seed_opencode_auth(bridge_dir)
    assert dest == bridge.xdg_data_home_for_bridge_dir(bridge_dir) / "opencode" / "auth.json"
    assert json.loads(dest.read_text()) == {"anthropic": {"type": "api", "key": "k"}}
    assert (os.stat(dest).st_mode & 0o777) == 0o600
    assert bridge.seeded_provider_ids(bridge_dir) == frozenset({"anthropic"})


def test_seed_opencode_auth_merges_v2_db_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v2-only logins live in SQLite; they are written in legacy shape so the
    per-session DB's one-time import picks them up."""
    share = _user_share(monkeypatch, tmp_path)
    (share / "opencode" / "auth.json").write_text(
        '{"anthropic": {"type": "api", "key": "stale"}, "groq": {"type": "api", "key": "g"}}'
    )
    monkeypatch.setattr(
        "omnigent.onboarding.opencode_auth.stored_v2_credentials",
        lambda db_path=None: {"anthropic": {"type": "api", "key": "fresh"}},
    )
    bridge_dir = bridge.prepare_bridge_dir("conv_merge")
    dest = bridge.seed_opencode_auth(bridge_dir)
    assert dest is not None
    assert json.loads(dest.read_text()) == {
        "anthropic": {"type": "api", "key": "fresh"},
        "groq": {"type": "api", "key": "g"},
    }


def test_seed_opencode_auth_merges_real_v2_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real v2 SQLite ``credential`` row (no mocking) merges into the seeded file."""
    import sqlite3

    share = _user_share(monkeypatch, tmp_path)
    (share / "opencode" / "auth.json").write_text('{"groq": {"type": "api", "key": "g"}}')
    db_path = share / "opencode" / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT NOT NULL,"
        " value TEXT NOT NULL, connector_id TEXT, method_id TEXT, active INTEGER,"
        " time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO credential VALUES ('cred_0', 'anthropic', 'x', ?, NULL, NULL, 1, 0, 1)",
        (json.dumps({"type": "key", "key": "fresh"}),),
    )
    conn.commit()
    conn.close()

    bridge_dir = bridge.prepare_bridge_dir("conv_real_db")
    dest = bridge.seed_opencode_auth(bridge_dir)
    assert dest is not None
    assert json.loads(dest.read_text()) == {
        "anthropic": {"type": "api", "key": "fresh"},
        "groq": {"type": "api", "key": "g"},
    }


def test_seed_opencode_auth_noop_without_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No auth.json and no v2 DB → None (e.g. on a remote runner)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-share"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    bridge_dir = bridge.prepare_bridge_dir("conv_noseed")
    assert bridge.seed_opencode_auth(bridge_dir) is None
    assert bridge.seeded_provider_ids(bridge_dir) == frozenset()


def test_user_opencode_config_path_default_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without XDG_CONFIG_HOME, looks at ~/.config/opencode/opencode.jsonc."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    fake_home = tmp_path / "fake_home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # File does not exist → returns None.
    assert user_opencode_config_path() is None
    # Create the file and verify the path is as expected.
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    (fake_home / ".config" / "opencode" / "opencode.jsonc").write_text("{}")
    expected = fake_home / ".config" / "opencode" / "opencode.jsonc"
    assert user_opencode_config_path() == expected


def test_user_opencode_config_path_honors_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """XDG_CONFIG_HOME env var redirects the lookup."""
    cfg_dir = tmp_path / "my-config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "my-config"))
    path = user_opencode_config_path()
    assert path is not None and path.exists()
    assert path.name == "opencode.jsonc"


def test_user_opencode_config_path_falls_back_to_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When only opencode.json exists (no .jsonc), returns the .json path."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = user_opencode_config_path()
    assert path is not None and path.name == "opencode.json"


def test_user_opencode_config_path_prefers_jsonc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When both .jsonc and .json exist, .jsonc is preferred."""
    cfg_dir = tmp_path / "pref" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text("{}", encoding="utf-8")
    (cfg_dir / "opencode.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "pref"))
    path = user_opencode_config_path()
    assert path is not None and path.name == "opencode.jsonc"


# ── owner-pid marker + orphan prune (bridge-dir reaping) ────────────────────


def test_prepare_bridge_dir_writes_owner_pid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_bridge_dir records the creating pid so the periodic sweep can
    prune the dir only when its owner is provably dead."""
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")

    bridge_dir = prepare_bridge_dir("bridge_owner")

    assert (bridge_dir / "owner.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_prepare_bridge_dir_excludes_concurrent_orphan_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement keeps its bridge and session data while preparation is active."""
    root = tmp_path / "opencode-native"
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", root)
    bridge_dir = bridge_dir_for_bridge_id("same-session")
    bridge_dir.mkdir(parents=True)
    owner_marker = bridge_dir / "owner.pid"
    owner_marker.write_text("999999", encoding="utf-8")
    session_data = xdg_data_home_for_bridge_dir(bridge_dir) / "storage" / "session.json"
    session_data.parent.mkdir(parents=True)
    session_data.write_text("preserve me", encoding="utf-8")

    marker_write_started = threading.Event()
    release_marker_write = threading.Event()
    real_write_owner_pid_marker = bridge.native_bridge_common.write_owner_pid_marker
    errors: list[BaseException] = []
    prepared_paths: list[Path] = []

    def _pause_before_owner_write(path: Path) -> None:
        marker_write_started.set()
        if not release_marker_write.wait(timeout=5.0):
            raise TimeoutError("test did not release owner-marker write")
        real_write_owner_pid_marker(path)

    def _prepare_replacement() -> None:
        try:
            prepared_paths.append(prepare_bridge_dir("same-session"))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(
        bridge.native_bridge_common,
        "write_owner_pid_marker",
        _pause_before_owner_write,
    )
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    prepare_thread = threading.Thread(target=_prepare_replacement)
    prepare_thread.start()
    try:
        assert marker_write_started.wait(timeout=5.0)
        assert bridge.prune_orphaned_bridge_dirs() == 0
    finally:
        release_marker_write.set()
        prepare_thread.join(timeout=5.0)

    assert not prepare_thread.is_alive()
    assert errors == []
    assert prepared_paths == [bridge_dir]
    assert bridge_dir.is_dir()
    assert session_data.read_text(encoding="utf-8") == "preserve me"
    assert owner_marker.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_prune_orphaned_bridge_dirs_only_removes_dead_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prune removes only provably-dead-owner dirs; live and unmarked survive."""
    import subprocess
    import sys

    root = tmp_path / "opencode-native"
    root.mkdir(parents=True)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", root)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    (dead_dir / "owner.pid").write_text(str(dead.pid), encoding="utf-8")

    live_dir = root / "liveowner"
    live_dir.mkdir()
    (live_dir / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")

    unmarked_dir = root / "unmarked"
    unmarked_dir.mkdir()

    pruned = bridge.prune_orphaned_bridge_dirs()

    assert pruned == 1
    assert not dead_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()


def test_last_applied_model_round_trips(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir, last_applied_model="acme/model-a"))
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model == "acme/model-a"
    raw = json.loads((bridge_dir / "state.json").read_text(encoding="utf-8"))
    assert raw["last_applied_model"] == "acme/model-a"


def test_last_applied_model_absent_in_older_state_reads_none(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir))
    path = bridge_dir / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("last_applied_model")
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model is None


def test_update_last_applied_model(bridge_dir: Path) -> None:
    assert update_last_applied_model(bridge_dir, "acme/model-a") is False  # no state yet
    write_bridge_state(bridge_dir, _state(bridge_dir, model_override="openai/gpt-5.5"))
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model is None

    assert update_last_applied_model(bridge_dir, " openai/gpt-5.5 ") is True
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model == "openai/gpt-5.5"
    assert loaded.model_override == "openai/gpt-5.5"

    assert update_last_applied_model(bridge_dir, None) is True
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model is None


class _KeyClient:
    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    async def connect_provider_key(self, provider_id: str, api_key: str) -> bool:
        self.calls.append((provider_id, api_key))
        if provider_id == self._fail:
            raise RuntimeError("boom")
        return True


async def test_connect_env_provider_keys_skips_stored_and_failures() -> None:
    client = _KeyClient(fail="groq")
    connected = await bridge.connect_env_provider_keys(
        client,
        stored={"anthropic"},
        environ={
            "ANTHROPIC_API_KEY": "a",
            "OPENAI_API_KEY": " o ",
            "GEMINI_API_KEY": "g1",
            "GOOGLE_GENERATIVE_AI_API_KEY": "g2",
            "GROQ_API_KEY": "q",
        },
    )
    assert connected == ["openai", "google"]
    # google connects once (first matching var); groq failed and is skipped.
    assert client.calls == [("openai", "o"), ("google", "g1"), ("groq", "q")]


async def test_connect_env_provider_keys_noop_without_env() -> None:
    client = _KeyClient()
    assert await bridge.connect_env_provider_keys(client, environ={}) == []
    assert client.calls == []


def _make_opencode_db(path: Path, *, claimed: bool) -> None:
    """Create a minimal v2-shaped OpenCode DB with one session row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, time_suspended INTEGER)")
        conn.execute(
            "INSERT INTO session_v2 (id, time_suspended) VALUES (?, ?)",
            ("ses_src", 1_700_000_000_000 if claimed else None),
        )
    conn.close()


def test_copy_opencode_database_for_fork_releases_claims(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    _make_opencode_db(opencode_db_path_for_bridge_dir(source_dir), claimed=True)

    assert copy_opencode_database_for_fork(source_dir, dest_dir) is True

    dest = opencode_db_path_for_bridge_dir(dest_dir)
    with sqlite3.connect(dest) as conn:
        rows = conn.execute("SELECT id, time_suspended FROM session_v2").fetchall()
    conn.close()
    assert rows == [("ses_src", None)], "a copied in-flight claim would re-run the source turn"
    assert (dest.stat().st_mode & 0o777) == 0o600
    # The source keeps its own claim; only the copy is released.
    with sqlite3.connect(opencode_db_path_for_bridge_dir(source_dir)) as conn:
        assert conn.execute("SELECT time_suspended FROM session_v2").fetchone()[0] is not None
    conn.close()


def test_copy_opencode_database_for_fork_missing_source(tmp_path: Path) -> None:
    (tmp_path / "dest").mkdir()
    assert copy_opencode_database_for_fork(tmp_path / "source", tmp_path / "dest") is False
    assert not opencode_db_path_for_bridge_dir(tmp_path / "dest").exists()


def test_copy_opencode_database_for_fork_unknown_schema_leaves_no_copy(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    with sqlite3.connect(opencode_db_path_for_bridge_dir(source_dir)) as conn:
        conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.close()

    assert copy_opencode_database_for_fork(source_dir, dest_dir) is False
    assert not opencode_db_path_for_bridge_dir(dest_dir).exists()
