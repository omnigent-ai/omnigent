"""Synthesize OpenCode provider config for the native-server harness.

Unlike codex/claude/pi — which consume ``HARNESS_*_GATEWAY_*`` env vars that
their CLIs translate into provider config — OpenCode reads its provider/auth
from its own config file under the per-session ``XDG_CONFIG_HOME``. So routing
opencode-native through the Databricks AI gateway (or any OpenAI-compatible
endpoint) means writing an ``opencode.json`` into the runner-owned
``opencode serve``'s config dir at spawn, declaring a custom
``@ai-sdk/openai-compatible`` provider pointed at ``{host}/serving-endpoints``.

The model is then referenced as ``<provider_id>/<endpoint>`` per prompt.

Security: the file carries a bearer token, so it is written ``0600`` into the
per-session XDG dir (never the user's global ``~/.config/opencode``). The token
is resolved at spawn; a resumed session re-spawns the server and re-resolves, so
short-lived gateway tokens refresh on resume (documented limitation: a token
that expires mid-session is not refreshed in place).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.spec.types import MCPServerConfig

_logger = logging.getLogger(__name__)

# Provider id used in the synthesized opencode.json for the Databricks gateway.
# The per-prompt model is pinned as ``{DATABRICKS_GATEWAY_PROVIDER_ID}/<endpoint>``.
DATABRICKS_GATEWAY_PROVIDER_ID = "databricks-gateway"
DATABRICKS_GATEWAY_PROVIDER_NAME = "Databricks AI Gateway"
# Endpoint that exposes the workspace's OpenAI-compatible chat completions.
_SERVING_ENDPOINTS_PATH = "serving-endpoints"
# Fallback chat model when neither the spec nor config names one.
DEFAULT_DATABRICKS_GATEWAY_MODEL = "databricks-claude-sonnet-4-6"


@dataclass(frozen=True)
class OpenCodeGatewayResolution:
    """A resolved OpenAI-compatible gateway for the opencode-native harness.

    :param base_url: OpenAI-compatible base URL, e.g.
        ``"https://ws.cloud.databricks.com/serving-endpoints"``.
    :param api_key: Bearer token / API key for the gateway.
    :param model_id: The endpoint/model id, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param provider_id: opencode provider id, e.g. ``"databricks-gateway"``.
    :param provider_name: Human label for the opencode provider block.
    """

    base_url: str
    api_key: str
    model_id: str
    provider_id: str = DATABRICKS_GATEWAY_PROVIDER_ID
    provider_name: str = DATABRICKS_GATEWAY_PROVIDER_NAME

    @property
    def qualified_model(self) -> str:
        """:returns: The per-prompt ``provider/model`` id opencode expects."""
        return f"{self.provider_id}/{self.model_id}"


def build_opencode_model_default_config(model: str) -> dict[str, object]:
    """
    Build a minimal ``opencode.json`` that only pins the default model.

    Used when the user's own provider auth (``opencode auth login`` /
    provider env keys) already supplies credentials, but a default model has
    been chosen — via ``omni opencode --model`` or the ``omni setup`` OpenCode
    default — so the per-session TUI (and the first turn) launch on that model
    instead of OpenCode's built-in default (``opencode/big-pickle``). No
    provider block: OpenCode resolves the provider from the model id's prefix
    against its own ``auth.json``.

    :param model: A ``provider/model`` id, e.g. ``"anthropic/claude-sonnet-4-5"``.
    :returns: A config dict ready to serialize to ``opencode.json``.
    """
    return {"$schema": "https://opencode.ai/config.json", "model": model}


def build_opencode_provider_config(resolution: OpenCodeGatewayResolution) -> dict[str, object]:
    """
    Build the ``opencode.json`` declaring a custom OpenAI-compatible provider.

    :param resolution: The resolved gateway (base URL + key + model).
    :returns: A config dict ready to serialize to ``opencode.json``.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            resolution.provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": resolution.provider_name,
                "options": {
                    "baseURL": resolution.base_url,
                    "apiKey": resolution.api_key,
                },
                "models": {resolution.model_id: {"name": resolution.model_id}},
            }
        },
    }


def write_opencode_provider_config(xdg_config_home: Path, config: Mapping[str, object]) -> Path:
    """
    Atomically write ``<xdg_config_home>/opencode/opencode.json`` (``0600``).

    :param xdg_config_home: The per-session ``XDG_CONFIG_HOME`` the server uses.
    :param config: The provider config dict (see
        :func:`build_opencode_provider_config`).
    :returns: The path written.
    """
    cfg_dir = xdg_config_home / "opencode"
    cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cfg_dir / "opencode.json"
    payload = json.dumps(config, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix="opencode.json.", dir=str(cfg_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def build_opencode_mcp_block(
    servers: Sequence[MCPServerConfig],
) -> dict[str, dict[str, object]]:
    """
    Translate Omnigent MCP server declarations into opencode.json's ``mcp`` block.

    Mirrors how codex/claude expose the agent's MCP servers, but via opencode's
    own config (no relay): ``stdio`` → ``{type:"local", command:[cmd, *args],
    environment, enabled}``; ``http`` → ``{type:"remote", url, headers,
    enabled}``. A ``databricks_profile`` resolves a bearer token into the
    ``Authorization`` header at spawn (re-resolved on resume, like the gateway
    provider). Entries opencode can't represent (missing command / url) are
    skipped.

    :param servers: The agent spec's ``mcp_servers``.
    :returns: An opencode ``mcp`` block keyed by server name (empty when none
        are representable).
    """
    block: dict[str, dict[str, object]] = {}
    for server in servers:
        name = getattr(server, "name", None)
        if not name:
            continue
        if getattr(server, "transport", "http") == "stdio":
            command = getattr(server, "command", None)
            if not command:
                continue
            entry: dict[str, object] = {
                "type": "local",
                "command": [command, *getattr(server, "args", [])],
                "enabled": True,
            }
            env = dict(getattr(server, "env", {}) or {})
            if env:
                entry["environment"] = env
        else:
            url = getattr(server, "url", None)
            if not url:
                continue
            headers = dict(getattr(server, "headers", {}) or {})
            profile = getattr(server, "databricks_profile", None)
            if profile and "Authorization" not in headers:
                token = _databricks_bearer_token(profile)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            entry = {"type": "remote", "url": url, "enabled": True}
            if headers:
                entry["headers"] = headers
        block[str(name)] = entry
    return block


def build_opencode_omnigent_mcp_server(
    bridge_dir: Path, *, python_executable: str | None = None
) -> dict[str, dict[str, object]]:
    """
    Build the opencode ``mcp`` entry that connects opencode to Omnigent's MCP.

    This is what makes opencode's model call the Omnigent builtin tools
    (``sys_session_*``, ``sys_agent_*``, ``load_skill``, ``web_fetch``,
    ``list_comments``/``update_comment``, policy tools, …). opencode launches the
    SHARED ``omnigent.claude_native_bridge serve-mcp`` as a ``{type:"local"}``
    stdio MCP server (the same relay codex/cursor/qwen use); ``serve-mcp`` reads
    the relay URL+token from ``tool_relay.json`` in *bridge_dir* (written by the
    runner's comment relay) and proxies each tool call back through the Omnigent
    server, where policy is enforced. The command is sourced from
    :func:`claude_native_bridge.build_mcp_config` so the invocation stays in one
    place.

    :param bridge_dir: OpenCode-native bridge directory (must hold ``bridge.json``
        + ``tool_relay.json``).
    :param python_executable: Python to run ``serve-mcp`` with; ``None`` uses the
        runner interpreter (has ``omnigent`` importable).
    :returns: A one-entry ``mcp`` block ``{"omnigent": {type:"local", …}}``.
    """
    from omnigent.claude_native_bridge import build_mcp_config

    claude_cfg = build_mcp_config(bridge_dir, python_executable=python_executable)
    # build_mcp_config returns {"mcpServers": {"<name>": {command, args, env}}};
    # opencode wants a flat command list + ``environment``.
    name, server = next(iter(claude_cfg["mcpServers"].items()))
    entry: dict[str, object] = {
        "type": "local",
        "command": [server["command"], *server.get("args", [])],
        "enabled": True,
    }
    env = dict(server.get("env", {}) or {})
    if env:
        entry["environment"] = env
    return {str(name): entry}


def _databricks_bearer_token(profile: str) -> str | None:
    """Resolve a bearer token for a ``~/.databrickscfg`` profile (best-effort)."""
    try:
        from databricks.sdk.core import Config

        headers = Config(profile=profile).authenticate() or {}
        authz = headers.get("Authorization", "")
        return authz.split(" ", 1)[1] if authz.lower().startswith("bearer ") else None
    except Exception as exc:  # noqa: BLE001 - SDK absent / bad profile / auth failure.
        _logger.info("opencode MCP databricks token resolve failed for %r: %r", profile, exc)
        return None


def resolve_databricks_gateway(
    profile: str | None,
    *,
    model_id: str | None = None,
) -> OpenCodeGatewayResolution | None:
    """
    Resolve a Databricks AI gateway for opencode from a ``~/.databrickscfg`` profile.

    Uses ``databricks-sdk`` (the ``databricks`` extra) to obtain the workspace
    host + a bearer token for *profile*, then targets the workspace's
    OpenAI-compatible ``/serving-endpoints``. Best-effort: returns ``None`` when
    the SDK is absent, the profile is unknown, or auth fails — the caller then
    leaves opencode on its ambient provider config.

    :param profile: A ``~/.databrickscfg`` profile name, e.g. ``"oss"``;
        ``None`` short-circuits.
    :param model_id: Endpoint/model id to pin; defaults to
        :data:`DEFAULT_DATABRICKS_GATEWAY_MODEL` (a ``databricks-*`` chat
        endpoint the gateway routes).
    :returns: A resolution, or ``None`` when the gateway can't be resolved.
    """
    if not profile:
        return None
    try:
        from databricks.sdk.core import Config

        config = Config(profile=profile)
        host = (config.host or "").rstrip("/")
        if not host:
            return None
        headers = config.authenticate() or {}
        authz = headers.get("Authorization", "")
        token = authz.split(" ", 1)[1] if authz.lower().startswith("bearer ") else ""
        if not token:
            return None
    except Exception as exc:  # noqa: BLE001 - SDK absent / auth failure / bad profile.
        _logger.info("opencode Databricks gateway resolve failed for %r: %r", profile, exc)
        return None

    resolved_model = _gateway_endpoint_for_model(model_id) or DEFAULT_DATABRICKS_GATEWAY_MODEL
    return OpenCodeGatewayResolution(
        base_url=f"{host}/{_SERVING_ENDPOINTS_PATH}",
        api_key=token,
        model_id=resolved_model,
    )


def _gateway_endpoint_for_model(model_id: str | None) -> str | None:
    """
    Normalize a spec model id to a Databricks serving-endpoint name.

    Accepts ``"databricks-claude-..."`` and ``"databricks/claude-..."`` spellings
    and strips a leading ``databricks/`` provider prefix; anything that does not
    look like a ``databricks-*`` endpoint is ignored (the gateway only routes
    its own endpoint names), so the default applies.

    :param model_id: The spec/override model id, or ``None``.
    :returns: A bare endpoint name, or ``None``.
    """
    if not model_id:
        return None
    candidate = model_id.split("/", 1)[1] if model_id.startswith("databricks/") else model_id
    return candidate if candidate.startswith("databricks-") else None


def _strip_jsonc_comments(text: str) -> str:
    """
    Strip ``//`` line comments and ``/* */`` block comments from JSONC text.

    Uses a character-level state machine to track string boundaries, so
    ``//`` inside string literals (e.g. URLs like ``"https://example.com"``)
    are never mistaken for comments.
    """
    result: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    string_char: str | None = None

    while i < length:
        ch = text[i]

        if in_string:
            if ch == "\\":
                result.append(ch)
                i += 1
                if i < length:
                    result.append(text[i])
                    i += 1
            elif ch == string_char:
                in_string = False
                result.append(ch)
                i += 1
            else:
                result.append(ch)
                i += 1
        elif ch in ('"', "'"):
            in_string = True
            string_char = ch
            result.append(ch)
            i += 1
        elif ch == "/" and i + 1 < length:
            next_ch = text[i + 1]
            if next_ch == "/":
                i += 2
                while i < length and text[i] != "\n":
                    i += 1
            elif next_ch == "*":
                i += 2
                while i + 1 < length:
                    if text[i] == "*" and text[i + 1] == "/":
                        i += 2
                        break
                    i += 1
            else:
                result.append(ch)
                i += 1
        else:
            result.append(ch)
            i += 1

    return "".join(result)


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before ``}`` or ``]`` (valid in JSONC, invalid in JSON).

    Operates on text that has already had its JSONC comments stripped, so the
    only commas present are real JSON commas.  Uses a character-level state
    machine that tracks string boundaries so that ``, }`` or ``, ]`` inside
    quoted values are never mistaken for trailing commas — preventing silent
    corruption of provider options like ``"note": "a, }"``.
    """
    result: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    string_char: str | None = None

    while i < length:
        ch = text[i]

        if in_string:
            if ch == "\\":
                result.append(ch)
                i += 1
                if i < length:
                    result.append(text[i])
                    i += 1
            elif ch == string_char:
                in_string = False
                result.append(ch)
                i += 1
            else:
                result.append(ch)
                i += 1
        elif ch in ('"', "'"):
            in_string = True
            string_char = ch
            result.append(ch)
            i += 1
        elif ch == ",":
            j = i + 1
            while j < length and text[j] in (" ", "\t", "\n", "\r"):
                j += 1
            if j < length and text[j] in ("}", "]"):
                i = j
            else:
                result.append(ch)
                i += 1
        else:
            result.append(ch)
            i += 1

    return "".join(result)


def maybe_merge_user_provider_config(config: dict[str, object]) -> dict[str, object]:
    """
    Merge the user's global OpenCode provider definitions into *config*.

    OpenCode reads ``XDG_CONFIG_HOME/opencode/opencode.json(c)`` for custom
    provider definitions (e.g. OpenAI-compatible endpoints with custom base
    URLs). When running under Omnigent, the per-session ``XDG_CONFIG_HOME``
    override hides this global config. This function reads the user's real
    config and merges any ``provider`` block into *config* so the spawned
    server sees both the user's providers (with their custom base URLs) and
    any Omnigent-synthesized providers (e.g. Databricks gateway).

    Only ``provider`` entries are merged — the synthesized config takes
    precedence for all other keys (model, mcp, plugin, permission, etc.).

    :param config: The synthesized config dict (may be empty).
    :returns: *config* with user's ``provider`` entries merged in (if any).
    """
    from omnigent.opencode_native_bridge import user_opencode_config_path

    user_path = user_opencode_config_path()
    if user_path is None:
        return config

    try:
        raw = user_path.read_text(encoding="utf-8")
        # Try plain JSON first (handles .json files without comments).
        # If that fails, strip JSONC comments and trailing commas, then
        # retry (handles .jsonc).
        try:
            user_config = json.loads(raw)
        except json.JSONDecodeError:
            cleaned = _strip_jsonc_comments(raw)
            cleaned = _strip_trailing_commas(cleaned)
            user_config = json.loads(cleaned)
    except (OSError, UnicodeDecodeError):
        return config
    except json.JSONDecodeError:
        _logger.warning(
            "Failed to parse user OpenCode config at %s — ignoring user providers",
            user_path,
        )
        return config

    if not isinstance(user_config, dict):
        return config

    user_providers = user_config.get("provider")
    if not isinstance(user_providers, dict) or not user_providers:
        return config

    result = dict(config)
    existing = result.get("provider")
    if isinstance(existing, dict):
        # Merge user's providers alongside existing ones; don't clobber
        # synthesized providers (Omnigent's keys like "databricks-gateway"
        # take priority).
        merged = dict(existing)
        for key, value in user_providers.items():
            if key not in merged:
                merged[key] = value
        result["provider"] = merged
    else:
        result["provider"] = dict(user_providers)

    result.setdefault("$schema", "https://opencode.ai/config.json")

    return result
