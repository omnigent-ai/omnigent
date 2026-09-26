"""Bridge state for native OpenCode (``opencode serve``) sessions.

The OpenCode native harness mirrors the Codex native bridge, but the
transport is HTTP + SSE instead of WebSocket JSON-RPC. The runner owns
the ``opencode serve`` process and the SSE forwarder; the harness-side
executor (spawned as a separate FastAPI process) reads this bridge state
to learn the loopback server URL, auth secret, and OpenCode session id so
it can inject web turns over REST.

Layout (per bridge id):

    ~/.omnigent/opencode-native/<sha256(bridge_id)[:32]>/
        state.json          # runtime state (mutates each turn)
        auth.secret         # OPENCODE_PASSWORD for this server
        xdg-data/           # XDG_DATA_HOME for the per-session opencode
        xdg-config/         # XDG_CONFIG_HOME for the per-session opencode

State (server URL, opencode session id, active message) is written by the
runner-owned server manager / forwarder and read by the harness executor;
the XDG dirs are preserved across runner restarts so a local resume keeps
OpenCode's persisted session history.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omnigent.native import native_bridge_common

_logger = logging.getLogger(__name__)

# Env var the runner stamps on the harness process so the executor can
# locate its bridge directory. Mirrors ``HARNESS_CODEX_NATIVE_BRIDGE_DIR``.
OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_OPENCODE_NATIVE_BRIDGE_DIR"
OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_OPENCODE_NATIVE_REQUEST_SESSION_ID"
# Label key recording the bridge id on the conversation, mirroring the
# codex-native ``omnigent.codex_native.bridge_id`` label.
OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY = "omnigent.opencode_native.bridge_id"

# OpenCode server password env. v2 reads OPENCODE_PASSWORD and still honors
# the legacy OPENCODE_SERVER_PASSWORD; both carry the per-session secret.
OPENCODE_PASSWORD_ENV_VAR = "OPENCODE_PASSWORD"
OPENCODE_SERVER_PASSWORD_ENV_VAR = "OPENCODE_SERVER_PASSWORD"
# The v2 server's fixed basic-auth username.
OPENCODE_DEFAULT_USERNAME = "opencode"
# Per-session SQLite store ``opencode serve`` keeps sessions and credentials in.
OPENCODE_DB_ENV_VAR = "OPENCODE_DB"
OPENCODE_DB_FILENAME = "opencode.db"

_STATE_FILE = "state.json"
_AUTH_SECRET_FILE = "auth.secret"
_XDG_DATA_DIR = "xdg-data"
_XDG_CONFIG_DIR = "xdg-config"
# Token file the shared ``omnigent.harnesses.claude_native.bridge serve-mcp`` reads to
# boot (filename MUST match ``claude_native_bridge._CONFIG_FILE``). opencode
# launches that serve-mcp as a ``{type:"local"}`` MCP server which relays the
# Omnigent builtin tools (``sys_*``/``load_skill``/``web_fetch``) advertised in
# ``tool_relay.json`` by the runner's comment relay.
_MCP_BRIDGE_CONFIG_FILE = "bridge.json"
# AP-routing snapshot the detached cost-approval popup process reads to resolve
# the elicitation against the Omnigent server (mirrors codex-native's
# ``policy_hook.json``; consumed by ``omnigent.native.native_cost_popup``).
_COST_POPUP_CONFIG_FILE = "cost_popup.json"
# Directory name (and plugin id) of the generated opencode policy plugin package.
OPENCODE_POLICY_PLUGIN_ID = "omnigent-policy"
_PLUGIN_ENTRYPOINT = "server.js"

# Default-exported plain object: ``Plugin.define`` is an identity function and a
# bridge-dir plugin cannot resolve ``@opencode/plugin`` (no node_modules above it).
# Raw string so JS escapes survive verbatim.
_OPENCODE_POLICY_PLUGIN_JS = r"""
// Omnigent policy bridge for opencode-native (generated; do not edit).
// Gates prompt submission (REQUEST) and tool output (TOOL_RESULT) through
// the Omnigent policy engine; tool calls are gated by permission.asked instead.
import fs from "node:fs"

const BASE = (process.env.OMNIGENT_POLICY_URL || "").replace(/\/+$/, "")
const SESSION = process.env.OMNIGENT_SESSION_ID || ""
const RELAY_FILE = process.env.OMNIGENT_RELAY_FILE || ""
let POLICY_HEADERS = {}
try {
  POLICY_HEADERS = JSON.parse(process.env.OMNIGENT_POLICY_HEADERS || "{}") || {}
} catch (_e) {
  POLICY_HEADERS = {}
}
const TIMEOUT_MS = 600000
const DENY = "POLICY_ACTION_DENY"

// tool_relay.json appears after the server starts, so re-read it per call.
function relayCredentials() {
  if (!RELAY_FILE) return null
  try {
    const d = JSON.parse(fs.readFileSync(RELAY_FILE, "utf8"))
    if (d && typeof d.url === "string" && typeof d.token === "string") {
      return { url: d.url, token: d.token }
    }
  } catch (_e) {}
  return null
}

async function evaluate(type, target, data) {
  // Unwired (no server/session) or any transport failure allows: fail open.
  if (!BASE || !SESSION) return { result: "ALLOW" }
  const relay = relayCredentials()
  const url = relay
    ? relay.url.replace(/\/+$/, "") + "/policies/evaluate"
    : BASE + "/v1/sessions/" + encodeURIComponent(SESSION) + "/policies/evaluate"
  const headers = relay
    ? { "content-type": "application/json", authorization: "Bearer " + relay.token }
    : { "content-type": "application/json", ...POLICY_HEADERS }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ event: { type, target: target || "", data } }),
      signal: controller.signal,
    })
    if (!resp.ok) return { result: "ALLOW" }
    const body = await resp.json()
    return body && typeof body === "object" ? body : { result: "ALLOW" }
  } catch (_e) {
    return { result: "ALLOW" }
  } finally {
    clearTimeout(timer)
  }
}

function resultText(result) {
  if (!result) return ""
  const content = result.content
  if (typeof content === "string") return content
  if (Array.isArray(content)) {
    return content
      .filter((part) => part && part.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n")
  }
  if (result.output === undefined) return ""
  try {
    return JSON.stringify(result.output)
  } catch (_e) {
    return String(result.output)
  }
}

export default {
  id: "omnigent-policy",
  setup: async (ctx) => {
    // REQUEST phase: a thrown error rejects the prompt before it is recorded.
    // Web-injected prompts were gated at injection, so the server allows them.
    await ctx.session.hook("prompt", async (event) => {
      const prompt = event && event.prompt
      const text = prompt && typeof prompt.text === "string" ? prompt.text : ""
      if (!text) return
      const verdict = await evaluate("PHASE_REQUEST", "", { text })
      if (verdict.result === DENY) {
        const reason = verdict.reason || "request denied"
        throw new Error("Omnigent policy blocked this prompt: " + reason)
      }
    })
    // TOOL_RESULT phase: the tool already ran; a DENY withholds its output.
    await ctx.tool.hook("execute.after", async (event) => {
      if (!event || event.status !== "completed") return
      const data = { result: resultText(event.result) }
      const verdict = await evaluate("PHASE_TOOL_RESULT", event.tool, data)
      if (verdict.result === DENY) {
        const reason = verdict.reason || "denied"
        event.result = { content: "[Omnigent policy withheld this tool result: " + reason + "]" }
      }
    })
  },
}
"""


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_plugin_package(root: Path, name: str, *, source: str) -> Path:
    """
    Write an ESM opencode plugin package ``<root>/<name>/{package.json,server.js}``.

    opencode 2.x rejects configured plugin paths that are files; a directory
    resolves its ``server`` entrypoint. ``"type": "module"`` makes ``server.js`` ESM.

    :param root: Parent directory (the bridge dir).
    :param name: Package directory and npm name, e.g. ``"omnigent-policy"``.
    :param source: The ``server.js`` module source.
    :returns: The package directory (register this path in ``plugins``).
    """
    package_dir = root / name
    package_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write_text(
        package_dir / "package.json",
        json.dumps({"name": name, "private": True, "type": "module"}, indent=2) + "\n",
    )
    _atomic_write_text(package_dir / _PLUGIN_ENTRYPOINT, source)
    return package_dir


def write_opencode_policy_plugin(bridge_dir: Path) -> Path:
    """
    Write the Omnigent policy-bridge plugin package and return its directory.

    The runner registers the directory in ``opencode.json`` ``plugins`` and stamps
    ``OMNIGENT_POLICY_URL`` / ``OMNIGENT_SESSION_ID`` / ``OMNIGENT_POLICY_HEADERS``
    / ``OMNIGENT_RELAY_FILE`` on ``opencode serve``. Overwritten each launch.

    :param bridge_dir: OpenCode-native bridge directory.
    :returns: ``<bridge_dir>/omnigent-policy``.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return write_plugin_package(
        bridge_dir, OPENCODE_POLICY_PLUGIN_ID, source=_OPENCODE_POLICY_PLUGIN_JS
    )


_STATE_VERSION = 1
_BRIDGE_ROOT = Path.home() / ".omnigent" / "opencode-native"
_ID_HASH_CHARS = 32


def bridge_root() -> Path:
    """
    Return the configured OpenCode-native bridge root.

    Tests may monkeypatch :data:`_BRIDGE_ROOT` to isolate bridge files.

    :returns: Absolute root for OpenCode-native bridge directories, e.g.
        ``Path("~/.omnigent/opencode-native")``.
    """
    return _BRIDGE_ROOT


@dataclass(frozen=True)
class OpenCodeNativeBridgeState:
    """
    Runtime state shared by the native OpenCode wrapper and harness.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param server_base_url: Loopback base URL of ``opencode serve``, e.g.
        ``"http://127.0.0.1:49231"``.
    :param opencode_session_id: OpenCode session id, e.g. ``"ses_abc123"``.
    :param auth_secret: ``OPENCODE_SERVER_PASSWORD`` for basic auth, or
        ``None`` when the server runs without auth.
    :param xdg_data_home: ``XDG_DATA_HOME`` the server runs with.
    :param xdg_config_home: ``XDG_CONFIG_HOME`` the server runs with.
    :param active_message_id: OpenCode assistant message id of the active
        turn, or ``None`` when idle.
    :param status: Coarse status, ``"idle"`` or ``"busy"``.
    :param model_override: Persisted model override, e.g.
        ``"anthropic/claude-opus-4"``, or ``None``.
    :param workspace: Workspace cwd the session runs in.
    :param last_event_id: Last SSE event id seen, for resume/debug.
    :param last_applied_model: Model most recently pushed to the OpenCode
        session via ``POST /api/session/{id}/model``, e.g.
        ``"opencode/big-pickle"``; ``None`` until the first switch.
    """

    session_id: str
    server_base_url: str
    opencode_session_id: str
    auth_secret: str | None = None
    xdg_data_home: str | None = None
    xdg_config_home: str | None = None
    active_message_id: str | None = None
    status: str = "idle"
    model_override: str | None = None
    workspace: str | None = None
    last_event_id: str | None = None
    last_applied_model: str | None = None

    def auth_headers(self) -> dict[str, str]:
        """
        Build basic-auth headers for the OpenCode server.

        :returns: ``{"Authorization": "Basic ..."}`` when an auth secret
            is set, otherwise an empty dict.
        """
        return auth_headers_for_secret(self.auth_secret)


def auth_headers_for_secret(secret: str | None) -> dict[str, str]:
    """
    Build OpenCode basic-auth headers for a server password.

    :param secret: The ``OPENCODE_SERVER_PASSWORD`` value, or ``None``.
    :returns: ``{"Authorization": "Basic <b64(user:secret)>"}`` or ``{}``.
    """
    if not secret:
        return {}
    raw = f"{OPENCODE_DEFAULT_USERNAME}:{secret}".encode()
    token = base64.b64encode(raw).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def bridge_dir_for_bridge_id(bridge_id: str) -> Path:
    """
    Return the bridge directory for an OpenCode-native bridge id.

    :param bridge_id: Opaque bridge id, e.g. ``"conv_abc123"``.
    :returns: Absolute bridge directory under
        ``~/.omnigent/opencode-native``.
    """
    digest = hashlib.sha256(bridge_id.encode("utf-8")).hexdigest()[:_ID_HASH_CHARS]
    return _BRIDGE_ROOT / digest


def build_opencode_native_spawn_env(
    conversation_id: str,
    *,
    bridge_id: str | None = None,
) -> dict[str, str]:
    """
    Build spawn env for the ``opencode-native`` harness process.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id; ``None`` uses *conversation_id*.
    :returns: Environment variables the OpenCode-native executor needs.
    """
    resolved_bridge_id = bridge_id or conversation_id
    return {
        OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_bridge_id(resolved_bridge_id)),
        OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }


def prepare_bridge_dir(bridge_id: str) -> Path:
    """
    Create the bridge directory (and XDG roots) for *bridge_id*.

    :param bridge_id: Opaque bridge id, e.g. ``"conv_abc123"``.
    :returns: Prepared absolute bridge directory.
    """
    bridge_dir = bridge_dir_for_bridge_id(bridge_id)
    with native_bridge_common.bridge_dir_preparation_lock(bridge_dir):
        bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(bridge_dir, 0o700)
        xdg_data_home_for_bridge_dir(bridge_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        xdg_config_home_for_bridge_dir(bridge_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        # Owner-pid marker for the periodic dead-owner prune; refreshed every
        # turn so it always names the current runner. See native_bridge_common.
        native_bridge_common.write_owner_pid_marker(bridge_dir)
    return bridge_dir


def prune_orphaned_bridge_dirs() -> int:
    """
    Remove opencode-native bridge dirs whose owner process is provably dead.

    Delegates to the shared sweep against this harness's bridge root; the
    global maintenance calls it (via ``native_bridge_common.reap_orphaned_native_bridge_dirs``)
    at startup to reclaim dirs leaked by a prior runner that died without
    running the explicit delete path.

    :returns: The number of orphaned bridge dirs removed.
    """
    return native_bridge_common.prune_orphaned_dirs(bridge_root())


def write_relay_bridge_config(bridge_dir: Path) -> None:
    """
    Write a minimal ``bridge.json`` so the shared ``serve-mcp`` can boot.

    The shared ``omnigent.harnesses.claude_native.bridge serve-mcp`` stdio server (which
    opencode launches as a ``{type:"local"}`` MCP server) reads this file for an
    auth token at startup; the relay tools themselves come from
    ``tool_relay.json`` (written by the runner's comment relay), so this carries
    only a token — no ``workspace`` key, so no ``sys_os_*`` tools are served
    (opencode owns its own filesystem tools). Mirrors
    ``codex_native_bridge.write_mcp_bridge_config``.

    Idempotent: skips if a config already exists so a relaunch never rotates a
    token the relay HTTP server was already started with.

    :param bridge_dir: OpenCode-native bridge directory.
    """
    config_path = bridge_dir / _MCP_BRIDGE_CONFIG_FILE
    if config_path.exists():
        return
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"token": secrets.token_urlsafe(32)}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_MCP_BRIDGE_CONFIG_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, config_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_cost_popup_config(
    bridge_dir: Path, *, ap_server_url: str, ap_auth_headers: dict[str, str]
) -> Path:
    """
    Write the AP-routing snapshot the cost-approval popup reads.

    The cost-budget approval modal runs as a detached
    ``omnigent.native.native_cost_popup`` subprocess inside a ``tmux display-popup`` on
    the opencode pane; it must POST the verdict to the Omnigent server but cannot
    inherit the forwarder's in-memory client, so the base URL + a one-shot auth
    header snapshot are persisted here (same contract as codex-native's
    ``policy_hook.json``). Rewritten on each checkpoint so the token is fresh.

    :param bridge_dir: OpenCode-native bridge directory.
    :param ap_server_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param ap_auth_headers: Outbound auth headers, e.g.
        ``{"Authorization": "Bearer <token>"}``; empty for no-auth local mode.
    :returns: The written config file path (passed to ``launch_cost_popup``).
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / _COST_POPUP_CONFIG_FILE
    payload = {"ap_server_url": ap_server_url, "ap_auth_headers": ap_auth_headers}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_COST_POPUP_CONFIG_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def xdg_data_home_for_bridge_dir(bridge_dir: Path) -> Path:
    """
    Return the per-session ``XDG_DATA_HOME`` for *bridge_dir*.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Absolute ``XDG_DATA_HOME`` directory.
    """
    return bridge_dir / _XDG_DATA_DIR


def xdg_config_home_for_bridge_dir(bridge_dir: Path) -> Path:
    """
    Return the per-session ``XDG_CONFIG_HOME`` for *bridge_dir*.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Absolute ``XDG_CONFIG_HOME`` directory.
    """
    return bridge_dir / _XDG_CONFIG_DIR


def opencode_db_path_for_bridge_dir(bridge_dir: Path) -> Path:
    """
    Return the per-session OpenCode SQLite path for *bridge_dir*.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Absolute ``opencode.db`` path, passed to the server as
        ``OPENCODE_DB``.
    """
    return bridge_dir / OPENCODE_DB_FILENAME


def _remove_database_files(database: Path) -> None:
    """Delete a SQLite database and its WAL/SHM side files, if present."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            database.with_name(database.name + suffix).unlink()


def copy_opencode_database_for_fork(source_bridge_dir: Path, dest_bridge_dir: Path) -> bool:
    """
    Snapshot a source conversation's OpenCode DB into a fork's bridge dir.

    The fork's own ``opencode serve`` must see the source session to run
    ``POST /api/session/{id}/fork``. The copy releases execution claims
    (``session_v2.time_suspended``) so the fork's server does not resume the
    source's unfinished turn on boot.

    :param source_bridge_dir: Bridge dir of the conversation being forked.
    :param dest_bridge_dir: Bridge dir of the new (forked) conversation.
    :returns: ``True`` when the copy is in place; ``False`` (and no copy left
        behind) when the source DB is missing or its schema is unrecognized.
    """
    source = opencode_db_path_for_bridge_dir(source_bridge_dir)
    if not source.is_file():
        return False
    dest = opencode_db_path_for_bridge_dir(dest_bridge_dir)
    try:
        dest_bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _remove_database_files(dest)
        # Pre-create with 0600 so SQLite's -wal/-shm siblings inherit the mode.
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with (
            # Read-only: a sole writer connection would checkpoint (and delete)
            # the source's WAL on close, discarding an uncheckpointed server.
            contextlib.closing(
                sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10.0)
            ) as src,
            contextlib.closing(sqlite3.connect(dest)) as dst,
        ):
            src.backup(dst)
            dst.execute("UPDATE session_v2 SET time_suspended = NULL")
            dst.commit()
    except (sqlite3.Error, OSError):
        _remove_database_files(dest)
        return False
    return True


def user_opencode_config_path() -> Path | None:
    """
    Return the user's real OpenCode config path (not the per-session one).

    Honors ``XDG_CONFIG_HOME`` (the runner's own env, which is the user's real
    config home — the per-session override is set only on the spawned server),
    defaulting to ``~/.config/opencode/opencode.jsonc``.

    OpenCode accepts both ``.jsonc`` (with comments) and ``.json`` extensions;
    the ``.jsonc`` variant is checked first.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    cfg_dir = base / "opencode"
    for name in ("opencode.jsonc", "opencode.json"):
        path = cfg_dir / name
        if path.is_file():
            return path
    return None


def _per_session_auth_path(bridge_dir: Path) -> Path:
    return xdg_data_home_for_bridge_dir(bridge_dir) / "opencode" / "auth.json"


def seed_opencode_auth(bridge_dir: Path) -> Path | None:
    """
    Write the user's OpenCode credentials into the per-session ``auth.json``.

    ``opencode serve`` runs with a per-session ``XDG_DATA_HOME`` and DB. OpenCode 2.x
    imports ``$XDG_DATA_HOME/opencode/auth.json`` once, when that DB is created, so
    seed it with the user's legacy ``auth.json`` plus their v2 SQLite credentials
    (legacy shape; the DB wins on conflicts). Written ``0600``; refreshed each spawn.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: The written path, or ``None`` when there are no credentials or the write fails.
    """
    from omnigent.onboarding.opencode_auth import opencode_auth_path, stored_v2_credentials

    merged: dict[str, object] = {}
    try:
        legacy = json.loads(opencode_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        legacy = None
    if isinstance(legacy, dict):
        merged.update({str(k): v for k, v in legacy.items() if v})
    merged.update(stored_v2_credentials())
    if not merged:
        return None
    dest = _per_session_auth_path(bridge_dir)
    try:
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="auth.json.", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, sort_keys=True)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, dest)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    except OSError:
        return None
    return dest


def seeded_provider_ids(bridge_dir: Path) -> frozenset[str]:
    """
    Return provider ids present in the per-session ``auth.json``.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Integration ids, empty when nothing was seeded.
    """
    try:
        data = json.loads(_per_session_auth_path(bridge_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(str(k) for k in data) if isinstance(data, dict) else frozenset()


class _ProviderKeyClient(Protocol):
    async def connect_provider_key(self, provider_id: str, api_key: str) -> bool: ...


async def connect_env_provider_keys(
    client: _ProviderKeyClient,
    *,
    stored: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """
    Store provider API keys from the environment in the per-session credential DB.

    Fallback for providers with no seeded credential: each provider whose API-key
    env var is set is connected with ``POST /api/integration/{id}/connect/key``.

    :param client: The per-session OpenCode client.
    :param stored: Provider ids that already have a credential.
    :param environ: Environment to read; ``None`` uses ``os.environ``.
    :returns: Provider ids connected.
    """
    from omnigent.onboarding.opencode_auth import _ENV_PROVIDER_VARS

    env = os.environ if environ is None else environ
    skip = set(stored)
    connected: list[str] = []
    for provider_id, _label, var in _ENV_PROVIDER_VARS:
        if provider_id in skip:
            continue
        key = env.get(var, "").strip()
        if not key:
            continue
        skip.add(provider_id)
        try:
            ok = await client.connect_provider_key(provider_id, key)
        except Exception:  # noqa: BLE001 - best effort; opencode can still read the env itself.
            _logger.info("opencode env key connect failed for %s", provider_id, exc_info=True)
            continue
        if ok:
            connected.append(provider_id)
    return connected


def auth_secret_path(bridge_dir: Path) -> Path:
    """
    Return the auth-secret file path for *bridge_dir*.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Absolute path of the ``auth.secret`` file.
    """
    return bridge_dir / _AUTH_SECRET_FILE


def ensure_auth_secret(bridge_dir: Path) -> str:
    """
    Read or mint the per-session OpenCode server password.

    The secret is reused across server restarts for one bridge dir so a
    resumed server keeps the same basic-auth credential the TUI/executor
    were configured with. Written ``0600``.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: The server password (``OPENCODE_SERVER_PASSWORD``).
    """
    path = auth_secret_path(bridge_dir)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        # No secret on disk yet: fall through to mint a fresh one below.
        pass
    except OSError:
        # Secret exists but is unreadable: ignore and regenerate it below.
        pass
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_AUTH_SECRET_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return secret


def state_path(bridge_dir: Path) -> Path:
    """
    Return the bridge state file path for *bridge_dir*.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Absolute path of the ``state.json`` file.
    """
    return bridge_dir / _STATE_FILE


def write_bridge_state(bridge_dir: Path, state: OpenCodeNativeBridgeState) -> None:
    """
    Persist shared native OpenCode state atomically.

    :param bridge_dir: Native OpenCode bridge directory.
    :param state: State payload to persist.
    :returns: None.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_path(bridge_dir)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_STATE_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": _STATE_VERSION,
                    "session_id": state.session_id,
                    "server_base_url": state.server_base_url,
                    "opencode_session_id": state.opencode_session_id,
                    "auth_secret": state.auth_secret,
                    "xdg_data_home": state.xdg_data_home,
                    "xdg_config_home": state.xdg_config_home,
                    "active_message_id": state.active_message_id,
                    "status": state.status,
                    "model_override": state.model_override,
                    "workspace": state.workspace,
                    "last_event_id": state.last_event_id,
                    "last_applied_model": state.last_applied_model,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def clear_bridge_state(bridge_dir: Path) -> None:
    """
    Remove stale native OpenCode runtime state for a bridge directory.

    New server launches reuse the same bridge directory for a conversation
    id, but the old ``state.json`` may point at a server URL from a
    previous process. Clear it before starting the new server so web
    message forwarding waits for the new launch to publish its current URL
    and session instead of injecting into stale state.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: None.
    """
    try:
        state_path(bridge_dir).unlink()
    except FileNotFoundError:
        return


def read_bridge_state(bridge_dir: Path) -> OpenCodeNativeBridgeState | None:
    """
    Read shared native OpenCode bridge state.

    Corrupt / partial JSON is treated as absent (returns ``None``) so a
    half-written file never crashes a turn.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Parsed state, or ``None`` when no valid state exists.
    """
    path = state_path(bridge_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    server_base_url = raw.get("server_base_url")
    opencode_session_id = raw.get("opencode_session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(server_base_url, str) or not server_base_url:
        return None
    if not isinstance(opencode_session_id, str) or not opencode_session_id:
        return None

    def _opt_str(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    status = raw.get("status")
    return OpenCodeNativeBridgeState(
        session_id=session_id,
        server_base_url=server_base_url,
        opencode_session_id=opencode_session_id,
        auth_secret=_opt_str("auth_secret"),
        xdg_data_home=_opt_str("xdg_data_home"),
        xdg_config_home=_opt_str("xdg_config_home"),
        active_message_id=_opt_str("active_message_id"),
        status=status if isinstance(status, str) and status else "idle",
        model_override=_opt_str("model_override"),
        workspace=_opt_str("workspace"),
        last_event_id=_opt_str("last_event_id"),
        last_applied_model=_opt_str("last_applied_model"),
    )


def update_active_message_id(
    bridge_dir: Path,
    active_message_id: str | None,
    *,
    status: str | None = None,
) -> None:
    """
    Update the active OpenCode message id (and optionally status).

    :param bridge_dir: Native OpenCode bridge directory.
    :param active_message_id: Active assistant message id, or ``None``.
    :param status: New coarse status (``"idle"`` / ``"busy"``); ``None``
        leaves the existing status untouched.
    :returns: None.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return
    import dataclasses

    write_bridge_state(
        bridge_dir,
        dataclasses.replace(
            state,
            active_message_id=active_message_id,
            status=status if status is not None else state.status,
        ),
    )


def update_last_event_id(bridge_dir: Path, last_event_id: str) -> None:
    """
    Record the last SSE event id seen by the forwarder.

    :param bridge_dir: Native OpenCode bridge directory.
    :param last_event_id: Last SSE event id, e.g. ``"evt_..."``.
    :returns: None.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return
    import dataclasses

    write_bridge_state(bridge_dir, dataclasses.replace(state, last_event_id=last_event_id))


def update_model_override(bridge_dir: Path, model_override: str | None) -> bool:
    """
    Persist a new per-session model override (Omnigent→opencode model switch).

    Before each web-injected prompt the transport compares ``model_override``
    with ``last_applied_model`` and calls ``POST /api/session/{id}/model`` when
    they differ, so updating it here switches the model on the NEXT injected
    turn. A blank/whitespace value clears the override (OpenCode keeps the
    model it last had).

    :param bridge_dir: Native OpenCode bridge directory.
    :param model_override: New qualified model id (``provider/model``), or
        ``None`` / blank to clear.
    :returns: ``True`` when the state existed and was updated, ``False`` when
        no bridge state is present (server not launched yet).
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return False
    import dataclasses

    normalized = model_override.strip() if isinstance(model_override, str) else None
    write_bridge_state(bridge_dir, dataclasses.replace(state, model_override=normalized or None))
    return True


def update_last_applied_model(bridge_dir: Path, model: str | None) -> bool:
    """
    Record the model most recently applied to the OpenCode session.

    :param bridge_dir: Native OpenCode bridge directory.
    :param model: Qualified model id that ``POST /api/session/{id}/model``
        accepted, e.g. ``"opencode/big-pickle"``, or ``None`` to clear.
    :returns: ``True`` when the state existed and was updated, ``False`` when
        no bridge state is present.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return False
    import dataclasses

    normalized = model.strip() if isinstance(model, str) else None
    write_bridge_state(
        bridge_dir, dataclasses.replace(state, last_applied_model=normalized or None)
    )
    return True
