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
        auth.secret         # OPENCODE_SERVER_PASSWORD for this server
        xdg-data/           # XDG_DATA_HOME for the per-session opencode
        xdg-config/         # XDG_CONFIG_HOME for the per-session opencode

State (server URL, opencode session id, active message) is written by the
runner-owned server manager / forwarder and read by the harness executor;
the XDG dirs are preserved across runner restarts so a local resume keeps
OpenCode's persisted session history.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Env var the runner stamps on the harness process so the executor can
# locate its bridge directory. Mirrors ``HARNESS_CODEX_NATIVE_BRIDGE_DIR``.
OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_OPENCODE_NATIVE_BRIDGE_DIR"
OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_OPENCODE_NATIVE_REQUEST_SESSION_ID"
# Label key recording the bridge id on the conversation, mirroring the
# codex-native ``omnigent.codex_native.bridge_id`` label.
OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY = "omnigent.opencode_native.bridge_id"

# OpenCode server basic-auth env vars (see opencode ``attach``/``serve``).
OPENCODE_SERVER_PASSWORD_ENV_VAR = "OPENCODE_SERVER_PASSWORD"
OPENCODE_SERVER_USERNAME_ENV_VAR = "OPENCODE_SERVER_USERNAME"
# Default basic-auth username opencode falls back to when unset.
OPENCODE_DEFAULT_USERNAME = "opencode"

_STATE_FILE = "state.json"
_AUTH_SECRET_FILE = "auth.secret"
_XDG_DATA_DIR = "xdg-data"
_XDG_CONFIG_DIR = "xdg-config"
# Token file the shared ``omnigent.claude_native_bridge serve-mcp`` reads to
# boot (filename MUST match ``claude_native_bridge._CONFIG_FILE``). opencode
# launches that serve-mcp as a ``{type:"local"}`` MCP server which relays the
# Omnigent builtin tools (``sys_*``/``load_skill``/``web_fetch``) advertised in
# ``tool_relay.json`` by the runner's comment relay.
_MCP_BRIDGE_CONFIG_FILE = "bridge.json"
# AP-routing snapshot the detached cost-approval popup process reads to resolve
# the elicitation against the Omnigent server (mirrors codex-native's
# ``policy_hook.json``; consumed by ``omnigent.native_cost_popup``).
_COST_POPUP_CONFIG_FILE = "cost_popup.json"
# Filename of the opencode plugin that bridges opencode's lifecycle hooks to the
# Omnigent policy engine (REQUEST + TOOL_RESULT phases the reactive
# ``permission.asked`` path can't reach).
_POLICY_PLUGIN_FILE = "omnigent-policy.js"

# The plugin source. opencode loads it (registered by absolute path in the
# synthesized ``opencode.json`` ``plugin`` field) and iterates the module's
# function exports as plugins (legacy shape). It reads its Omnigent coordinates
# from env the runner stamps on ``opencode serve`` and POSTs each hook to
# ``/v1/sessions/{id}/policies/evaluate`` — the SAME endpoint + ``PHASE_*``
# contract claude-native's ``UserPromptSubmit`` / ``PostToolUse`` hooks use.
# Best-effort: any transport error fails OPEN (never locks the session); only an
# explicit ``POLICY_ACTION_DENY`` blocks a prompt (throw) or withholds a tool
# result (redact). Raw string so the JS ``\n`` / regex escapes survive verbatim.
_OPENCODE_POLICY_PLUGIN_JS = r"""
// Omnigent policy bridge for opencode-native (generated; do not edit).
// Forwards opencode lifecycle hooks to the Omnigent policy engine so
// REQUEST-phase (prompt-submit) and TOOL_RESULT-phase policies enforce — the
// phases the reactive permission.asked path cannot reach.
const BASE = (process.env.OMNIGENT_POLICY_URL || "").replace(/\/+$/, "");
const SESSION = process.env.OMNIGENT_SESSION_ID || "";
const AUTH = process.env.OMNIGENT_POLICY_AUTH || "";
const TIMEOUT_MS = 600000;

async function evaluate(type, target, data) {
  // Returns {result, reason}. Not wired (no server/session) -> no-op allow.
  if (!BASE || !SESSION) return { result: "ALLOW" };
  const url = BASE + "/v1/sessions/" + encodeURIComponent(SESSION) + "/policies/evaluate";
  const headers = { "content-type": "application/json" };
  if (AUTH) headers["authorization"] = AUTH;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify({ event: { type: type, target: target || "", data: data } }),
      signal: controller.signal,
    });
    if (!resp.ok) return { result: "ALLOW" };
    const body = await resp.json();
    return body && typeof body === "object" ? body : { result: "ALLOW" };
  } catch (e) {
    // Server unreachable / timeout: fail OPEN so a transient blip can't lock
    // the session. The web approval card (if any) stays parked server-side.
    return { result: "ALLOW" };
  } finally {
    clearTimeout(timer);
  }
}

function joinText(parts) {
  if (!Array.isArray(parts)) return "";
  const out = [];
  for (const p of parts) {
    if (p && p.type === "text" && typeof p.text === "string") out.push(p.text);
  }
  return out.join("\n");
}

export const OmnigentPolicyPlugin = async () => ({
  // REQUEST phase: gate the prompt before the model sees it. A DENY throws,
  // which opencode surfaces as an aborted turn (true block). On a web-injected
  // prompt the server auto-allows (it was gated at injection), so this only
  // gates TUI-typed prompts.
  "chat.message": async (_input, output) => {
    const text = output ? joinText(output.parts) : "";
    if (!text) return;
    // ``data`` is the {"text": ...} dict the server's _build_evaluation_context
    // expects for REQUEST (same shape claude's UserPromptSubmit hook sends);
    // a bare string 500s the evaluate endpoint and fails the gate open.
    const verdict = await evaluate("PHASE_REQUEST", "", { text: text });
    if (verdict.result === "POLICY_ACTION_DENY") {
      // opencode renders any thrown chat.message error as a generic 500 in the
      // TUI ("Unexpected server error") — its middleware hardcodes that. We
      // can't change the TUI text from a plugin, but the thrown message is
      // written to opencode's session log, so carry the policy reason there.
      throw new Error(
        "Omnigent policy blocked this prompt: " + (verdict.reason || "request denied"),
      );
    }
  },
  // TOOL_RESULT phase: gate/redact the tool output before the model sees it.
  // The tool already ran; a DENY withholds its output (the TOOL_RESULT-phase
  // suppress semantics) rather than aborting the turn.
  "tool.execute.after": async (input, output) => {
    if (!output) return;
    const verdict = await evaluate(
      "PHASE_TOOL_RESULT",
      input && input.tool,
      { result: output.output },
    );
    if (verdict.result === "POLICY_ACTION_DENY") {
      output.output = "[Omnigent policy withheld this tool result: " +
        (verdict.reason || "denied") + "]";
    }
  },
});
"""


def write_opencode_policy_plugin(bridge_dir: Path) -> Path:
    """
    Write the Omnigent policy-bridge plugin into *bridge_dir* and return its path.

    The runner registers the returned path in the synthesized ``opencode.json``
    ``plugin`` field and stamps ``OMNIGENT_POLICY_URL`` / ``OMNIGENT_SESSION_ID``
    / ``OMNIGENT_POLICY_AUTH`` on the ``opencode serve`` process so the plugin
    can reach ``/policies/evaluate``. Overwritten each launch so a code update
    ships without stale plugin files.

    :param bridge_dir: OpenCode-native bridge directory.
    :returns: The written plugin file path (absolute).
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / _POLICY_PLUGIN_FILE
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_POLICY_PLUGIN_FILE}.", dir=str(bridge_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_OPENCODE_POLICY_PLUGIN_JS)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


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
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(bridge_dir, 0o700)
    xdg_data_home_for_bridge_dir(bridge_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    xdg_config_home_for_bridge_dir(bridge_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    return bridge_dir


def write_relay_bridge_config(bridge_dir: Path) -> None:
    """
    Write a minimal ``bridge.json`` so the shared ``serve-mcp`` can boot.

    The shared ``omnigent.claude_native_bridge serve-mcp`` stdio server (which
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
    ``omnigent.native_cost_popup`` subprocess inside a ``tmux display-popup`` on
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


def user_opencode_auth_path() -> Path:
    """
    Return the user's real OpenCode ``auth.json`` path (not the per-session one).

    Honors ``XDG_DATA_HOME`` (the runner's own env, which is the user's real
    data home — the per-session override is set only on the spawned server),
    defaulting to ``~/.local/share/opencode/auth.json``.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode" / "auth.json"


def seed_opencode_auth(bridge_dir: Path) -> Path | None:
    """
    Copy the user's OpenCode ``auth.json`` into the per-session ``XDG_DATA_HOME``.

    The runner spawns ``opencode serve`` with a per-session ``XDG_DATA_HOME``
    that isolates session state — but it also hides the user's
    ``opencode auth login`` credentials (in their real
    ``~/.local/share/opencode/auth.json``). Without those, the server can only
    reach OpenCode's no-auth default model (``opencode/big-pickle``), so a
    user-selected provider/model never takes effect. Copy the credentials in
    (best-effort, ``0600``) so the user's providers — and any pinned model that
    needs them — work. Refreshed on every spawn so re-logins propagate.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: The destination path written, or ``None`` when there is no
        source ``auth.json`` or the copy fails.
    """
    src = user_opencode_auth_path()
    if not src.is_file():
        return None
    dest_dir = xdg_data_home_for_bridge_dir(bridge_dir) / "opencode"
    try:
        dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        dest = dest_dir / "auth.json"
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
    except OSError:
        return None
    return dest


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
    required = (session_id, server_base_url, opencode_session_id)
    if not all(isinstance(value, str) and value for value in required):
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

    opencode has no session-level model setting — the model is a per-prompt
    field — so the executor reads ``model_override`` from this bridge state on
    every web-injected prompt (see
    ``OpenCodeNativeExecutor._build_prompt_with_model_override``). Updating it
    here makes the NEXT injected turn use the new model. A blank/whitespace
    value clears the override (fall back to opencode's own default).

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
