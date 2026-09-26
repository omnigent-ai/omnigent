"""Synthesize the OpenCode 2.x ``opencode.json`` for the native-server harness.

The runner-owned ``opencode serve`` reads its config from the per-session
``XDG_CONFIG_HOME``. This module emits the v2 keys: ``providers`` (an
OpenAI-compatible gateway declared with the native
``@opencode/ai/providers/openai-compatible`` package), ``model``
(``provider/model``), an ask-all ``permissions`` ruleset, ``mcp.servers``,
``plugins`` and ``instructions``.

Security: the file carries a bearer token, so it is written ``0600`` into the
per-session XDG dir (never the user's global ``~/.config/opencode``). The token
is resolved at spawn; a resumed session re-spawns the server and re-resolves, so
short-lived gateway tokens refresh on resume (a token that expires mid-session
is not refreshed in place).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.models import model_catalog

if TYPE_CHECKING:
    from databricks.sdk.core import Config

    from omnigent.spec.types import MCPServerConfig

_logger = logging.getLogger(__name__)

# Provider id used in the synthesized opencode.json for the Databricks gateway.
# The per-prompt model is pinned as ``{DATABRICKS_GATEWAY_PROVIDER_ID}/<endpoint>``.
DATABRICKS_GATEWAY_PROVIDER_ID = "databricks-gateway"
DATABRICKS_GATEWAY_PROVIDER_NAME = "Databricks AI Gateway"
# Endpoint that exposes the workspace's OpenAI-compatible chat completions.
_SERVING_ENDPOINTS_PATH = "serving-endpoints"
# Optional deployment default: a ``databricks-*`` serving-endpoint id used when a
# session pins no compatible model. Unset falls back to the Databricks Claude
# catalog. Set it in the runner env to steer every session at one endpoint
# (e.g. ``databricks-kimi-k3``).
DATABRICKS_GATEWAY_DEFAULT_MODEL_ENV_VAR = "OMNIGENT_DATABRICKS_GATEWAY_MODEL"

OPENCODE_CONFIG_SCHEMA = "https://opencode.ai/config.json"
# Native provider package bundled with opencode 2.x (no npm install at runtime).
OPENAI_COMPATIBLE_PACKAGE = "@opencode/ai/providers/openai-compatible"
# Every tool call raises ``permission.asked`` so the forwarder can apply Omnigent policy.
ASK_ALL_PERMISSIONS: list[dict[str, str]] = [{"action": "*", "resource": "*", "effect": "ask"}]


@dataclass(frozen=True)
class OpenCodeGatewayResolution:
    """A resolved OpenAI-compatible gateway for the opencode-native harness.

    :param base_url: OpenAI-compatible base URL, e.g.
        ``"https://ws.cloud.databricks.com/serving-endpoints"``.
    :param api_key: Bearer token / API key for the gateway.
    :param model_id: The endpoint/model id, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param model_ids: Every serving-endpoint the gateway can route to, so opencode's
        in-session model picker lists them all. ``model_id`` stays the pinned launch
        default. Empty falls back to just ``model_id``.
    :param provider_id: opencode provider id, e.g. ``"databricks-gateway"``.
    :param provider_name: Human label for the opencode provider block.
    """

    base_url: str
    api_key: str
    model_id: str
    model_ids: tuple[str, ...] = ()
    provider_id: str = DATABRICKS_GATEWAY_PROVIDER_ID
    provider_name: str = DATABRICKS_GATEWAY_PROVIDER_NAME

    @property
    def qualified_model(self) -> str:
        """:returns: The per-prompt ``provider/model`` id opencode expects."""
        return f"{self.provider_id}/{self.model_id}"


def resolve_bound_opencode_gateway(
    *, model: str | None = None, auth: object = None
) -> OpenCodeGatewayResolution | None:
    """Resolve an explicit session binding without consulting Connect fallbacks."""
    from omnigent.inference_config import (
        binding_for_harness,
        load_runtime_inference_config,
        resolve_bound_model,
        resolve_bound_provider,
    )
    from omnigent.onboarding.provider_config import OPENAI_FAMILY

    config = load_runtime_inference_config()
    entry = resolve_bound_provider(config, "opencode-native", auth)
    if entry is None:
        return None
    family = entry.family(OPENAI_FAMILY)
    selected = resolve_bound_model(config, "opencode-native", model)
    if family is None or not selected:
        raise ValueError("OpenCode requires an OpenAI-compatible provider and a default model.")
    if family.wire_api == "responses":
        raise ValueError("OpenCode's configured gateway must support the chat wire API.")
    token = model_catalog._resolve_bearer_token(
        model_catalog.ResolvedModelProvider(
            kind=entry.kind, api_key=family.api_key, auth_command=family.auth_command
        )
    )
    binding = binding_for_harness(config, "opencode-native")
    return OpenCodeGatewayResolution(
        base_url=family.base_url,
        api_key=token,
        model_id=selected,
        model_ids=binding.model_allowlist or () if binding is not None else (),
        provider_id="omnigent",
        provider_name=entry.name,
    )


def build_opencode_provider_block(
    resolution: OpenCodeGatewayResolution,
) -> dict[str, dict[str, object]]:
    """
    Build the ``providers`` entry for an OpenAI-compatible gateway.

    :param resolution: The resolved gateway (base URL + key + models).
    :returns: ``{provider_id: {name, package, settings, models}}``.
    """
    return {
        resolution.provider_id: {
            "name": resolution.provider_name,
            "package": OPENAI_COMPATIBLE_PACKAGE,
            "settings": {
                "baseURL": resolution.base_url,
                "apiKey": resolution.api_key,
                "provider": resolution.provider_id,
            },
            "models": {
                mid: {"name": mid} for mid in (resolution.model_ids or (resolution.model_id,))
            },
        }
    }


def _permission_rules(extra: Sequence[Mapping[str, str]] | None) -> list[dict[str, str]]:
    """Ask-all first, then caller ``deny`` rules; other effects would bypass the gate."""
    rules = [dict(rule) for rule in ASK_ALL_PERMISSIONS]
    for rule in extra or ():
        if rule.get("effect") == "deny":
            rules.append(dict(rule))
        else:
            _logger.info("opencode config: dropping non-deny permission rule %r", dict(rule))
    return rules


def build_opencode_config(
    *,
    model: str | None,
    gateway: OpenCodeGatewayResolution | None,
    mcp_servers: Mapping[str, Mapping[str, object]],
    plugin_paths: Sequence[str],
    instructions: str | None,
    permissions: Sequence[Mapping[str, str]] | None = None,
    extra_providers: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """
    Build the per-session v2 ``opencode.json``.

    :param model: Default ``provider/model``; replaced by the gateway's model when set.
        Dropped (with a log line) when it lacks a ``provider/`` prefix — v2 only
        accepts ``provider/model``.
    :param gateway: Resolved OpenAI-compatible gateway, or ``None``. Wins over
        *model* and *extra_providers* on a provider id collision.
    :param mcp_servers: ``mcp.servers`` map (see :func:`build_opencode_mcp_block`).
    :param plugin_paths: Plugin package directories, in load order; de-duplicated.
    :param instructions: Path of an already-written instructions file (e.g. the
        per-session ``AGENTS.md`` from Task 56). This is NOT the system prompt:
        opencode 2.0.18 parses ``instructions`` but never reads it at runtime, so
        this only records the key when the caller explicitly passes a path — no
        prompt content is derived from it here.
    :param permissions: Extra rules; only ``deny`` rules are kept, appended after
        the mandatory ask-all rule (an ``allow``/``ask`` rule after ask-all would
        let a tool run without ``permission.asked``).
    :param extra_providers: Already-v2 provider entries (e.g. the managed ucode
        config), merged before the gateway's own provider block.
    :returns: The config dict.
    """
    config: dict[str, object] = {
        "$schema": OPENCODE_CONFIG_SCHEMA,
        "permissions": _permission_rules(permissions),
    }
    providers: dict[str, object] = {k: dict(v) for k, v in (extra_providers or {}).items()}
    if gateway is not None:
        providers.update(build_opencode_provider_block(gateway))
        model = gateway.qualified_model
    if providers:
        config["providers"] = providers
    if model and "/" in model:
        config["model"] = model
    elif model:
        _logger.info("opencode config: ignoring model %r without a provider prefix", model)
    if mcp_servers:
        config["mcp"] = {"servers": {name: dict(entry) for name, entry in mcp_servers.items()}}
    if plugin_paths:
        config["plugins"] = list(dict.fromkeys(plugin_paths))
    if instructions:
        config["instructions"] = [instructions]
    return config


def write_opencode_provider_config(xdg_config_home: Path, config: Mapping[str, object]) -> Path:
    """
    Atomically write ``<xdg_config_home>/opencode/opencode.json`` (``0600``).

    :param xdg_config_home: The per-session ``XDG_CONFIG_HOME`` the server uses.
    :param config: The v2 config dict (see :func:`build_opencode_config`).
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


_INSTRUCTIONS_FILE = "AGENTS.md"


def _user_agents_md() -> str | None:
    """The user's global ``~/.config/opencode/AGENTS.md`` text, if any."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    try:
        text = (base / "opencode" / _INSTRUCTIONS_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text.strip() or None


def write_opencode_instructions(xdg_config_home: Path, instructions: str | None) -> Path | None:
    """
    Write the per-session global ``AGENTS.md`` opencode loads as ambient instructions.

    opencode 2.0 parses the config ``instructions`` key but does not apply it; the
    global ``AGENTS.md`` under its config dir is always read. The user's own
    global ``AGENTS.md`` comes first so the per-session config dir does not hide it.

    :param xdg_config_home: The per-session ``XDG_CONFIG_HOME``.
    :param instructions: Raw author instructions, or ``None``.
    :returns: The written path, or ``None`` (and any stale file removed) when empty.
    """
    cfg_dir = xdg_config_home / "opencode"
    path = cfg_dir / _INSTRUCTIONS_FILE
    parts = [part for part in (_user_agents_md(), (instructions or "").strip()) if part]
    if not parts:
        path.unlink(missing_ok=True)
        return None
    cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_INSTRUCTIONS_FILE}.", dir=str(cfg_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n\n".join(parts) + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def _mcp_timeout(seconds: object) -> dict[str, int] | None:
    """Convert an ``MCPServerConfig.timeout`` in seconds to v2 ``{catalog, execution}`` ms."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    millis = int(seconds * 1000)
    return {"catalog": millis, "execution": millis}


def build_opencode_mcp_block(
    servers: Sequence[MCPServerConfig],
) -> dict[str, dict[str, object]]:
    """
    Translate Omnigent MCP server declarations into v2 ``mcp.servers`` entries.

    ``stdio`` → ``{type:"local", command:[cmd, *args], environment}``; ``http`` →
    ``{type:"remote", url, headers}``. A ``databricks_profile`` resolves a bearer
    token into ``Authorization`` at spawn. Every entry sets ``codemode: false`` so
    each tool keeps its name and is individually permission-gated. Entries
    without a command / url are skipped.

    :param servers: The agent spec's ``mcp_servers``.
    :returns: A ``mcp.servers`` map keyed by server name.
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
                "codemode": False,
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
            entry = {"type": "remote", "url": url, "codemode": False}
            if headers:
                entry["headers"] = headers
            if "Authorization" in headers:
                entry["oauth"] = False
        timeout = _mcp_timeout(getattr(server, "timeout", None))
        if timeout is not None:
            entry["timeout"] = timeout
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
    SHARED ``omnigent.harnesses.claude_native.bridge serve-mcp`` as a ``{type:"local"}``
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
    :returns: A one-entry ``mcp.servers`` map ``{"omnigent": {type:"local", codemode: False, …}}``.
    """
    from omnigent.harnesses.claude_native.bridge import (
        _TOOL_RELAY_POST_TIMEOUT_S,
        build_mcp_config,
    )

    claude_cfg = build_mcp_config(bridge_dir, python_executable=python_executable)
    # build_mcp_config returns {"mcpServers": {"<name>": {command, args, env}}};
    # opencode wants a flat command list + ``environment``.
    servers = claude_cfg.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        raise ValueError("Claude MCP config is missing mcpServers")
    name, server = next(iter(servers.items()))
    if not isinstance(server, dict):
        raise ValueError("Claude MCP server config is malformed")
    command = server.get("command")
    args = server.get("args", [])
    if (
        not isinstance(command, str)
        or not isinstance(args, list)
        or not all(isinstance(arg, str) for arg in args)
    ):
        raise ValueError("Claude MCP server command is malformed")
    entry: dict[str, object] = {
        "type": "local",
        "command": [command, *args],
        # Relay tools keep their names so each call raises its own permission.asked.
        "codemode": False,
        # Execution deadline in ms: longer than the bridge's outer relay hop so the
        # relay's own timeout error arrives before opencode kills the call.
        "timeout": {"execution": int((_TOOL_RELAY_POST_TIMEOUT_S + 30.0) * 1000)},
    }
    env_value = server.get("env")
    if env_value is None:
        env: dict[str, str] = {}
    elif isinstance(env_value, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in env_value.items()
    ):
        env = dict(env_value)
    else:
        raise ValueError("Claude MCP server environment is malformed")
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
    :param model_id: Endpoint/model id to pin. When omitted or incompatible,
        the Databricks Claude catalog supplies the endpoint.
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

    resolved_model = _gateway_endpoint_for_model(model_id)
    if resolved_model is None:
        resolved_model = _gateway_endpoint_for_model(
            os.environ.get(DATABRICKS_GATEWAY_DEFAULT_MODEL_ENV_VAR)
        )
    if resolved_model is None:
        resolved_model = model_catalog.resolve_catalog_model(
            "databricks", family="claude"
        ).model_id
    # List every chat serving-endpoint so opencode's picker offers them all,
    # with the pinned default first. Best-effort: a failure leaves just the
    # default (dict.fromkeys de-dupes if the default is also discovered).
    model_ids = tuple(dict.fromkeys((resolved_model, *_list_gateway_models(config))))
    return OpenCodeGatewayResolution(
        base_url=f"{host}/{_SERVING_ENDPOINTS_PATH}",
        api_key=token,
        model_id=resolved_model,
        model_ids=model_ids,
    )


def _list_gateway_models(config: Config) -> tuple[str, ...]:
    """
    List the workspace's chat-capable ``databricks-*`` serving endpoints.

    Reuses the already-authenticated SDK *config* so it shares the gateway's
    auth. Best-effort: any failure (SDK absent, list denied) returns ``()`` and
    the caller falls back to the single pinned model. Embedding/rerank endpoints
    are dropped so the model picker only lists chat models.

    :param config: A ``databricks.sdk.core.Config`` for the gateway workspace.
    :returns: Endpoint names, e.g. ``("databricks-kimi-k3", ...)``.
    """
    try:
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient(config=config)
        ids: list[str] = []
        for endpoint in client.serving_endpoints.list():
            name = getattr(endpoint, "name", "") or ""
            if not name.startswith("databricks-"):
                continue
            task = (getattr(endpoint, "task", "") or "").lower()
            if "embed" in task or "rerank" in task:
                continue
            ids.append(name)
        return tuple(ids)
    except Exception as exc:  # noqa: BLE001 - SDK absent / list denied / bad shape.
        _logger.info("opencode Databricks gateway model list failed: %r", exc)
        return ()


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


_AISDK_PREFIX = "aisdk:"


def _read_user_opencode_config() -> dict[str, object] | None:
    """Parse the user's global ``opencode.json(c)``; ``None`` when absent or invalid."""
    from omnigent.harnesses.opencode_native.bridge import user_opencode_config_path

    user_path = user_opencode_config_path()
    if user_path is None:
        return None
    try:
        raw = user_path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = json.loads(_strip_trailing_commas(_strip_jsonc_comments(raw)))
    except (OSError, UnicodeDecodeError):
        return None
    except json.JSONDecodeError:
        _logger.warning("Failed to parse user OpenCode config at %s — ignoring it", user_path)
        return None
    return parsed if isinstance(parsed, dict) else None


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def _v1_model_to_v2(model: Mapping[str, object]) -> dict[str, object]:
    """Subset of opencode's v1 model migration (``migrate.ts:304-354``)."""
    out: dict[str, object] = {}
    if isinstance(model.get("name"), str):
        out["name"] = model["name"]
    if isinstance(model.get("id"), str):
        out["modelID"] = model["id"]
    headers = _string_map(model.get("headers"))
    if headers:
        out["headers"] = headers
    options = model.get("options")
    if isinstance(options, Mapping) and options:
        out["settings"] = dict(options)
    limit = model.get("limit")
    if isinstance(limit, Mapping):
        limits = {
            key: int(limit[key])
            for key in ("context", "input", "output")
            if isinstance(limit.get(key), (int, float)) and not isinstance(limit.get(key), bool)
        }
        if limits:
            out["limit"] = limits
    return out


def v1_provider_to_v2(entry: Mapping[str, object]) -> dict[str, object]:
    """
    Convert a v1 ``provider.<id>`` entry to a v2 ``providers.<id>`` entry.

    Mirrors opencode's own migration (``migrate.ts:247-260``): ``npm`` becomes an
    ``aisdk:``-prefixed ``package``, ``options`` becomes ``settings`` with
    ``headers``/``body`` lifted out, and ``api`` becomes ``settings.baseURL``.

    :param entry: The v1 provider object.
    :returns: The v2 provider object.
    """
    out: dict[str, object] = {}
    if isinstance(entry.get("name"), str):
        out["name"] = entry["name"]
    env = entry.get("env")
    if isinstance(env, list) and all(isinstance(item, str) for item in env):
        out["env"] = list(env)
    npm = entry.get("npm")
    if isinstance(npm, str) and npm:
        out["package"] = npm if npm.startswith(_AISDK_PREFIX) else _AISDK_PREFIX + npm
    options = entry.get("options")
    options = options if isinstance(options, Mapping) else {}
    settings = {str(k): v for k, v in options.items() if k not in ("headers", "body")}
    if isinstance(entry.get("api"), str):
        settings["baseURL"] = entry["api"]
    if settings:
        out["settings"] = settings
    headers = _string_map(options.get("headers"))
    if headers:
        out["headers"] = headers
    body = options.get("body")
    if isinstance(body, Mapping) and body:
        out["body"] = dict(body)
    models = entry.get("models")
    if isinstance(models, Mapping):
        out["models"] = {
            str(mid): _v1_model_to_v2(model)
            for mid, model in models.items()
            if isinstance(model, Mapping)
        }
    return out


def _user_providers(user: Mapping[str, object]) -> dict[str, object]:
    providers: dict[str, object] = {}
    legacy = user.get("provider")
    if isinstance(legacy, Mapping):
        for pid, entry in legacy.items():
            if isinstance(entry, Mapping):
                providers[str(pid)] = v1_provider_to_v2(entry)
    native = user.get("providers")
    if isinstance(native, Mapping):
        for pid, entry in native.items():
            if isinstance(entry, Mapping):
                providers[str(pid)] = dict(entry)
    return providers


def _user_model(user: Mapping[str, object]) -> str | None:
    model = user.get("model")
    if isinstance(model, str) and "/" in model:
        return model
    if isinstance(model, Mapping):
        provider_id, model_id = model.get("providerID"), model.get("model")
        if isinstance(provider_id, str) and isinstance(model_id, str):
            variant = model.get("variant")
            suffix = f"#{variant}" if isinstance(variant, str) and variant else ""
            return f"{provider_id}/{model_id}{suffix}"
    return None


def _user_plugins(user: Mapping[str, object]) -> list[object]:
    plugins: list[object] = []
    legacy = user.get("plugin")
    for item in legacy if isinstance(legacy, list) else []:
        if isinstance(item, str) and item:
            plugins.append(item)
        elif (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], Mapping)
        ):
            plugins.append({"package": item[0], "options": dict(item[1])})
    native = user.get("plugins")
    for item in native if isinstance(native, list) else []:
        if isinstance(item, str) and item:
            plugins.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("package"), str):
            plugins.append(dict(item))
    return plugins


def _v1_mcp_to_v2(entry: Mapping[str, object]) -> dict[str, object]:
    """Subset of opencode's v1 MCP migration (``migrate.ts:202-227``)."""
    out = {str(k): v for k, v in entry.items() if k not in ("enabled", "timeout")}
    enabled = entry.get("enabled")
    if isinstance(enabled, bool):
        out["disabled"] = not enabled
    timeout = entry.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        out["timeout"] = {"catalog": timeout, "execution": timeout}
    return out


def _user_mcp_servers(user: Mapping[str, object]) -> dict[str, object]:
    mcp = user.get("mcp")
    if not isinstance(mcp, Mapping):
        return {}
    servers: dict[str, object] = {}
    for name, entry in mcp.items():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("type") in ("local", "remote"):
            servers[str(name)] = _v1_mcp_to_v2(entry)
    native = mcp.get("servers")
    if isinstance(native, Mapping) and native.get("type") not in ("local", "remote"):
        for name, entry in native.items():
            if isinstance(entry, Mapping):
                servers[str(name)] = dict(entry)
    # Keep merged servers out of Code Mode so each call is asked as `<server>_<tool>`.
    for entry in servers.values():
        if isinstance(entry, dict):
            entry["codemode"] = False
    return servers


def maybe_merge_user_provider_config(config: dict[str, object]) -> dict[str, object]:
    """
    Merge the user's global OpenCode config into the synthesized v2 config.

    The per-session ``XDG_CONFIG_HOME`` hides ``~/.config/opencode``, so carry over
    the user's providers, default model, plugins and MCP servers. Both v1
    (``provider``, ``plugin``, flat ``mcp``) and v2 (``providers``, ``plugins``,
    ``mcp.servers``) spellings are read; the result uses v2 keys only.
    Synthesized entries always win; the user's model applies only when none is set.

    :param config: The synthesized config dict.
    :returns: A new dict with the user's entries merged in.
    """
    user = _read_user_opencode_config()
    if user is None:
        return config
    result = dict(config)

    user_providers = _user_providers(user)
    if user_providers:
        existing = result.get("providers")
        merged = dict(existing) if isinstance(existing, Mapping) else {}
        for pid, entry in user_providers.items():
            merged.setdefault(pid, entry)
        result["providers"] = merged

    user_model = _user_model(user)
    if user_model:
        result.setdefault("model", user_model)

    user_plugins = _user_plugins(user)
    if user_plugins:
        existing_plugins = result.get("plugins")
        plugins: list[object] = (
            list(existing_plugins) if isinstance(existing_plugins, list) else []
        )
        for plugin in user_plugins:
            if plugin not in plugins:
                plugins.append(plugin)
        result["plugins"] = plugins

    user_servers = _user_mcp_servers(user)
    if user_servers:
        mcp = result.get("mcp")
        if isinstance(mcp, Mapping) and "servers" not in mcp:
            # Pre-Task-63 callers may still hand us a flat ``mcp`` map (server
            # entries at the top level, not yet nested under ``servers``).
            # Lift it so the written config always uses the v2 shape.
            mcp_block: dict[str, object] = {}
            servers = dict(mcp)
        else:
            mcp_block = dict(mcp) if isinstance(mcp, Mapping) else {}
            current = mcp_block.get("servers")
            servers = dict(current) if isinstance(current, Mapping) else {}
        for name, entry in user_servers.items():
            servers.setdefault(name, entry)
        mcp_block["servers"] = servers
        result["mcp"] = mcp_block

    result.setdefault("$schema", OPENCODE_CONFIG_SCHEMA)
    return result


def _configure_opencode_on_demand() -> None:
    """Run ``ucode configure --agents opencode`` for the connect profile, minting
    via the broker. Used when the background boot configure has not yet written
    opencode's config at launch time. Best-effort and quiet."""
    from omnigent.host.databricks_credential import HOST_DATABRICKS_PROFILE, broker_token_command
    from omnigent.inner.databricks_executor import _read_databrickscfg_host
    from omnigent.onboarding.ucode_setup import (
        build_ucode_configure_command_for_profile,
        find_ucode_command,
        ucode_configure_lock,
    )

    host = _read_databrickscfg_host(HOST_DATABRICKS_PROFILE)
    bearer_command = broker_token_command(host.rstrip("/")) if host else None
    if not bearer_command:
        return
    ucode_config = Path.home() / ".ucode" / "opencode-xdg" / "opencode" / "opencode.json"
    try:
        with ucode_configure_lock():
            # The boot-time all-agent configure may have written opencode's config
            # while we waited for the lock — skip a redundant run if so.
            if ucode_config.exists():
                return
            argv = build_ucode_configure_command_for_profile(
                find_ucode_command(), profile=HOST_DATABRICKS_PROFILE, agents=["opencode"]
            )
            configure_env = {
                **os.environ,
                "DATABRICKS_BEARER_COMMAND": bearer_command,
                "DATABRICKS_CONFIG_PROFILE": HOST_DATABRICKS_PROFILE,
            }
            # ucode has no use for the host's launch token; don't hand it over.
            configure_env.pop("OMNIGENT_HOST_TOKEN", None)
            subprocess.run(argv, capture_output=True, timeout=120, env=configure_env)
    except Exception:  # noqa: BLE001 - best-effort; the caller declines if config is still absent.
        _logger.info("opencode on-demand ucode configure failed", exc_info=True)


UCODE_AUTH_PLUGIN_ID = "omnigent-ucode-auth"
_UCODE_AUTH_COMMAND_RE = re.compile(r"^const AUTH_COMMAND = (\[.*\])\s*$", re.MULTILINE)

_UCODE_AUTH_PLUGIN_JS = """// Databricks token refresh (generated by Omnigent; do not edit).
// Mints via ucode's auth-token command and stamps each model HTTP request.
import { execFile } from "node:child_process"
import { promisify } from "node:util"

const PROVIDERS = __PROVIDERS__
const AUTH_COMMAND = __AUTH_COMMAND__
const REFRESH_SKEW_MS = 120_000
const run = promisify(execFile)

let accessToken
let expiresAt = 0
let refreshPromise

function cacheToken(value) {
  accessToken = value
  expiresAt = Infinity
  try {
    const claims = JSON.parse(Buffer.from(value.split(".")[1], "base64url").toString())
    if (typeof claims.exp === "number") expiresAt = claims.exp * 1000
  } catch {}
}

async function mintToken() {
  try {
    const { stdout } = await run(AUTH_COMMAND[0], AUTH_COMMAND.slice(1), { encoding: "utf8" })
    const token = stdout.trim()
    if (!token) throw new Error("returned an empty token")
    cacheToken(token)
  } catch (error) {
    const detail = String(error.stderr || error.message || "").trim()
    throw new Error("ucode auth-token failed" + (detail ? ": " + detail : ""))
  }
}

function refreshToken() {
  if (!refreshPromise) refreshPromise = mintToken().finally(() => { refreshPromise = undefined })
  return refreshPromise
}

export default {
  id: "omnigent-ucode-auth",
  setup: async (ctx) => {
    for (const providerID of PROVIDERS) {
      await ctx.session.hook(
        "http.request",
        async (event) => {
          if (!accessToken || expiresAt <= Date.now() + REFRESH_SKEW_MS) await refreshToken()
          const headers = new Headers(event.request.headers)
          headers.set("Authorization", "Bearer " + accessToken)
          event.request = new Request(event.request, { headers })
        },
        { providerID },
      )
      // A 401 means the cached token is stale; the next retry mints a fresh one.
      await ctx.session.hook(
        "http.response",
        (event) => {
          if (event.response.status === 401) expiresAt = 0
        },
        { providerID },
      )
    }
  },
}
"""


def render_ucode_auth_plugin(*, providers: Sequence[str], auth_command: Sequence[str]) -> str:
    """
    Render the v2 plugin that stamps ucode-minted Databricks tokens on model requests.

    :param providers: opencode provider ids to authenticate, e.g. ``["databricks-oss"]``.
    :param auth_command: ucode's mint argv, e.g. ``["ucode", "auth-token", "--force-refresh"]``.
    :returns: The ``server.js`` source.
    """
    return _UCODE_AUTH_PLUGIN_JS.replace("__PROVIDERS__", json.dumps(list(providers))).replace(
        "__AUTH_COMMAND__", json.dumps(list(auth_command))
    )


def _ucode_auth_command(plugin_source: str) -> list[str] | None:
    """Extract ``AUTH_COMMAND`` from ucode's generated (v1) ``ucode-auth.js``."""
    match = _UCODE_AUTH_COMMAND_RE.search(plugin_source)
    if match is None:
        return None
    try:
        argv = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    return argv


def _provider_base_urls_match_host(config: Mapping[str, object], workspace_host: str) -> bool:
    """True when every v2 provider ``settings.baseURL`` is HTTPS on *workspace_host*.

    Guards the managed-connect path: a broker bearer is forwarded to whatever base
    URL the on-disk config names, so a stale/other origin must not be trusted.
    Requires at least one base URL.
    """
    from omnigent.host.databricks_credential import https_url_on_workspace_host

    providers = config.get("providers")
    if not isinstance(providers, Mapping):
        return False
    saw_url = False
    for provider in providers.values():
        settings = provider.get("settings") if isinstance(provider, Mapping) else None
        base_url = settings.get("baseURL") if isinstance(settings, Mapping) else None
        if not isinstance(base_url, str) or not base_url:
            continue
        saw_url = True
        if not https_url_on_workspace_host(base_url, workspace_host):
            return False
    return saw_url


def managed_connect_opencode_config(
    xdg_config_home: Path, bridge_dir: Path
) -> dict[str, object] | None:
    """Build v2 config fragments from ucode's opencode output on a managed connect host.

    ucode (run at host boot) writes a v1 ``opencode.json`` and a v1
    ``plugin/ucode-auth.js``. The provider blocks are converted to v2 and an
    Omnigent-owned v2 auth plugin reusing ucode's ``AUTH_COMMAND`` is written into
    *bridge_dir*. The caller must forward ``DATABRICKS_BEARER_COMMAND`` into the
    opencode env so ``ucode auth-token`` can mint.

    :param xdg_config_home: Per-session ``XDG_CONFIG_HOME`` (stale v1 plugin copies removed here).
    :param bridge_dir: Bridge dir that receives the ``omnigent-ucode-auth`` plugin package.
    :returns: ``{"model"?, "providers", "plugins"}``, or ``None`` off a managed host
        or when ucode's output is unusable.
    """
    from omnigent.harnesses.opencode_native.bridge import write_plugin_package
    from omnigent.host.databricks_credential import _read_sidecar, _sidecar_path

    sidecar = _read_sidecar(_sidecar_path())
    if sidecar is None:
        return None  # not a managed connect host
    workspace_host = sidecar["workspace_host"].rstrip("/")

    # ucode writes opencode's config into its own XDG root, not ~/.config/opencode.
    ucode_config_dir = Path.home() / ".ucode" / "opencode-xdg" / "opencode"
    ucode_config = ucode_config_dir / "opencode.json"
    if not ucode_config.exists():
        # The boot-time configure may not have run yet; configure on demand.
        _configure_opencode_on_demand()
    try:
        raw = json.loads(ucode_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _logger.info("opencode managed config: unreadable ucode config %s: %r", ucode_config, exc)
        return None
    raw_providers = raw.get("provider") if isinstance(raw, dict) else None
    if not isinstance(raw_providers, Mapping) or not raw_providers:
        _logger.info(
            "opencode managed config: ucode did not configure opencode (no provider block in %s); "
            "opencode falls back to its own login.",
            ucode_config,
        )
        return None
    providers = {
        str(pid): v1_provider_to_v2(entry)
        for pid, entry in raw_providers.items()
        if isinstance(entry, Mapping)
    }
    config: dict[str, object] = {"providers": providers}
    # Security: only forward a broker bearer to HTTPS base URLs on the sidecar's host.
    if not _provider_base_urls_match_host(config, workspace_host):
        _logger.warning(
            "opencode managed config: provider base URL is not HTTPS on the connected "
            "workspace host %r — declining so the broker bearer is not forwarded to an "
            "unverified origin.",
            workspace_host,
        )
        return None

    ucode_plugin = ucode_config_dir / "plugin" / "ucode-auth.js"
    try:
        auth_command = _ucode_auth_command(ucode_plugin.read_text(encoding="utf-8"))
    except OSError as exc:
        _logger.info("opencode managed config: unreadable ucode plugin %s (%r)", ucode_plugin, exc)
        auth_command = None
    if auth_command is None:
        _logger.info(
            "opencode managed config: no AUTH_COMMAND in %s; declining so no static bearer is "
            "used.",
            ucode_plugin,
        )
        return None

    # A v1 copy from an older launch would be auto-discovered and fail to load.
    (xdg_config_home / "opencode" / "plugin" / "ucode-auth.js").unlink(missing_ok=True)
    plugin_dir = write_plugin_package(
        bridge_dir,
        UCODE_AUTH_PLUGIN_ID,
        source=render_ucode_auth_plugin(providers=list(providers), auth_command=auth_command),
    )
    config["plugins"] = [str(plugin_dir)]
    model = raw.get("model")
    if isinstance(model, str) and "/" in model:
        config["model"] = model
    return config
