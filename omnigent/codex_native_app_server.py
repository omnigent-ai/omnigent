"""Codex app-server process and JSON-RPC client for native TUI sessions."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import socket
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, cast

import tomlkit
import websockets
from cachetools import TTLCache
from websockets.asyncio.client import ClientConnection

from omnigent import model_catalog
from omnigent.json_types import JsonObject as _JsonObject

if TYPE_CHECKING:
    from omnigent.onboarding.provider_config import ProviderEntry
    from omnigent.spec.types import AgentSpec

from omnigent.codex_model_vocabulary import codex_spawn_model
from omnigent.codex_native_bridge import write_policy_hook_config
from omnigent.codex_native_process_registry import (
    CodexNativeProcessOwnerLock,
    acquire_codex_native_process_owner_lock,
    codex_native_session_tag_cmdline_arg,
    reconcile_codex_native_process_registry,
    register_codex_native_process,
    unregister_codex_native_process,
)
from omnigent.inner import _proc
from omnigent.inner.codex_executor import (
    _CODEX_ROUTER_HOOK_MODULE,
    _clean_codex_env,
    _codex_cli_version,
    _codex_home_config_source_from_env,
    _databricks_codex_auth_command,
    _databricks_codex_base_url,
    _databricks_codex_config_overrides,
    _find_codex_cli,
    _populate_codex_home_config,
    _provider_codex_config_overrides,
    codex_extended_catalog_requested,
    codex_router_bridge_dir,
    codex_router_hooks_settings,
    codex_router_session_id,
    codex_routing_hook_skip_reason,
    materialize_codex_provider_config,
    read_codex_model_catalog,
    write_codex_hooks_file,
)
from omnigent.inner.databricks_executor import _databricks_gateway_host

_logger = logging.getLogger(__name__)

CodexMessage: TypeAlias = _JsonObject
CodexParams: TypeAlias = _JsonObject
# A bound app-server JSON-RPC request coroutine (``client.request`` or the
# SDK executor's ``_request``), so the trust helpers work over either transport.
CodexRequestFn = Callable[[str, CodexParams], Awaitable[CodexMessage]]

_CONNECT_RETRY_DELAY_SECONDS = 0.05
_CONNECT_TIMEOUT_SECONDS = 10.0
_MODEL_DISCOVERY_CACHE_SECONDS = 300.0
_STDERR_CHUNK_LIMIT = 65536
_UDS_WEBSOCKET_HANDSHAKE_URI = "ws://localhost/rpc"
_MAX_WEBSOCKET_MESSAGE_SIZE_BYTES = 128 << 20
# hooks.json filename written into the private CODEX_HOME registering the
# Omnigent policy hook. Codex discovers it as a ``user``-layer hook
# source on every config load (see codex hooks ``discover_handlers``).
_CODEX_HOOKS_FILE = "hooks.json"
# Module the codex policy command hook runs. Also the marker used to
# identify *our* hooks in ``hooks/list`` output so the trust step never
# auto-trusts unrelated hooks that the user's symlinked ``config.toml``
# might declare.
_POLICY_HOOK_MODULE = "omnigent.codex_native_hook"
# Codex-side timeout for the policy hook subprocess. A TOOL_CALL/LLM_REQUEST
# ASK is now resolved server-side: ``POST /policies/evaluate`` parks the gate
# (URL-based elicitation) until a human answers or the deciding policy's
# ``ask_timeout`` elapses, and the hook's own request budget
# (``_EVALUATE_POLICY_TIMEOUT_S``) is a day to match. Codex must wait at least
# as long, or it kills the hook mid-park and the tool runs before the verdict
# arrives — exactly the bug that let sub-agent tool calls slip past the cost
# gate. Held at a day so the server-side ``ask_timeout`` is the single cap,
# mirroring claude-native's ``PermissionRequest`` hook (``timeout: 86400``).
_POLICY_HOOK_TIMEOUT_SECONDS = 86400
# Hook trust statuses that allow a hook to execute (see codex
# ``hook_trust_status``). Anything else means the hook is silently
# skipped — which for a policy gate is a fail-open we must reject.
_TRUSTED_HOOK_STATUSES = frozenset({"trusted", "managed"})
# Minimum codex CLI version whose ``hooks/list`` returns the
# ``currentHash`` / ``trustStatus`` fields the trust handshake needs.
# Below this codex never exposes those fields, so the policy hook can
# never be trusted (it just stays ``untrusted`` and is silently skipped)
# — we detect the old version up front and skip registration with a loud
# warning rather than crash startup on an un-trustable hook.
_MIN_POLICY_HOOK_CODEX_VERSION = (0, 129, 0)
# Minimum codex CLI version that accepts ``--dangerously-bypass-hook-trust``.
# Older binaries exit immediately on the unknown flag, so below this floor
# (including a version we could not parse) the flag is omitted and the
# interactive trust prompt may appear instead.
_MIN_BYPASS_HOOK_TRUST_CODEX_VERSION = (0, 131, 0)
_MODEL_MIGRATION_CATALOG_TIMEOUT_SECONDS = 3.0


def _string_object_dict(value: object) -> _JsonObject | None:
    """Return *value* as a string-keyed object mapping when valid."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast("_JsonObject", value)


def _object_list(value: object) -> list[object] | None:
    """Return *value* as an object list when valid."""
    if not isinstance(value, list):
        return None
    return cast("list[object]", value)


def _format_codex_version(version: tuple[int, int, int] | None) -> str:
    """
    Render a parsed codex version tuple for log / error messages.

    :param version: Version tuple, e.g. ``(0, 129, 0)``, or ``None`` when
        the version could not be determined.
    :returns: A dotted string, e.g. ``"0.129.0"``, or ``"unknown"`` for
        ``None``.
    """
    if version is None:
        return "unknown"
    return ".".join(str(part) for part in version)


def _toml_table_header_name(line: str) -> str | None:
    """
    Return the TOML table name declared by *line*, if any.

    This intentionally recognizes only normal table headers because the
    injected Codex MCP server config is a normal table. Array tables are
    left untouched.

    :param line: One config line, e.g.
        ``"[mcp_servers.omnigent] # generated\n"``.
    :returns: The table name, e.g. ``"mcp_servers.omnigent"``, or
        ``None`` when *line* is not a normal table header.
    """
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    if stripped.startswith("[["):
        return None
    end = stripped.find("]")
    if end < 0:
        return None
    suffix = stripped[end + 1 :].strip()
    if suffix and not suffix.startswith("#"):
        return None
    return stripped[1:end].strip()


def _remove_toml_table(text: str, table_name: str) -> str:
    """
    Remove one TOML table and its subtables from a config document.

    Used for generated private Codex config before appending the
    Omnigent MCP server table. This avoids accumulating duplicate
    ``[mcp_servers.omnigent]`` sections across terminal relaunches.

    :param text: TOML document text.
    :param table_name: Table name to remove, e.g.
        ``"mcp_servers.omnigent"``.
    :returns: TOML text with the target table block removed.
    """
    kept: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        header = _toml_table_header_name(line)
        if header is not None:
            skipping = header == table_name or header.startswith(f"{table_name}.")
        if not skipping:
            kept.append(line)
    return "".join(kept).rstrip()


#: Omnigent tools the framework calls on every session's behalf, pre-approved
#: so codex never raises an interactive prompt for them. The rename keeps a
#: session's title current, which the framework does unprompted on any session.
_FRAMEWORK_APPROVED_TOOLS: tuple[str, ...] = ("sys_session_rename",)

#: Additionally pre-approved for an auto-harness Smart Routing session, whose
#: spawns the router may move onto the counterpart harness family: these four
#: carry out that cross-harness redirect end to end — discover the agent, start
#: the routed child, deliver the task, collect its result. Without the last one
#: the redirect stalls on an approval prompt nobody is watching. A plain or
#: pinned session can never receive a redirect, so it gets none of them and its
#: approval surface stays a plain codex session's. Mirrors the claude-native
#: ``_ROUTED_SPAWN_ALLOWED_TOOLS`` gate.
_ROUTED_SPAWN_APPROVED_TOOLS: tuple[str, ...] = (
    "sys_session_create",
    "sys_agent_list",
    "sys_session_send",
    "sys_read_inbox",
)


def framework_approved_tools(*, routed_spawns: bool) -> tuple[str, ...]:
    """
    Name the Omnigent tools this session pre-approves in codex.

    :param routed_spawns: ``True`` for an auto-harness Smart Routing session,
        which also needs the cross-harness redirect toolkit.
    :returns: Tool names, in the order their approval tables are written.
    """
    if not routed_spawns:
        return _FRAMEWORK_APPROVED_TOOLS
    return (*_FRAMEWORK_APPROVED_TOOLS, *_ROUTED_SPAWN_APPROVED_TOOLS)


def _codex_mcp_server_config_section(
    bridge_dir: Path,
    python_executable: str | None = None,
    *,
    routed_spawns: bool = False,
) -> str:
    """
    Build the generated Codex MCP server TOML section.

    :param bridge_dir: Bridge directory containing ``bridge.json`` and
        ``tool_relay.json``.
    :param python_executable: Python executable for serve-mcp, e.g.
        ``"/path/to/.venv/bin/python"``. ``None`` uses
        :data:`sys.executable`.
    :param routed_spawns: ``True`` for an auto-harness Smart Routing session,
        which pre-approves the cross-harness redirect tools too.
    :returns: TOML text for ``[mcp_servers.omnigent]`` and its
        framework-managed tool approvals.
    """
    python = python_executable or sys.executable
    args = [
        "-I",
        "-m",
        "omnigent.claude_native_bridge",
        "serve-mcp",
        "--bridge-dir",
        str(bridge_dir),
    ]
    args_toml = ", ".join(json.dumps(a) for a in args)
    approvals = "\n".join(
        f'[mcp_servers.omnigent.tools.{tool}]\napproval_mode = "approve"\n'
        for tool in framework_approved_tools(routed_spawns=routed_spawns)
    )
    return (
        f"[mcp_servers.omnigent]\n"
        f"command = {json.dumps(python)}\n"
        f"args = [{args_toml}]\n\n"
        f"{approvals}"
    )


# Top-level ``model_reasoning_effort = "<value>"`` line, capturing the value so
# it can be clamped to one the pinned model accepts. Tolerates a trailing comment.
_EFFORT_KEY_RE = re.compile(r'^(\s*model_reasoning_effort\s*=\s*")([^"]*)("\s*(?:#.*)?)$')


def _pin_codex_config_model(codex_home: Path, model: str) -> None:
    """
    Write *model* as the top-level ``model`` key in the session config.toml.

    The per-session ``config.toml`` starts as a copy of the user's shared
    one, so its ``model`` line is whatever the user last ran — NOT this
    session's launch model. The forwarder mirrors that file into
    ``model_override`` and the cost gate's hook reads it, so without this
    seed a per-dispatch model override is silently misreported (live-caught:
    a child launched on ``databricks-gpt-5-4-mini`` was mirrored back as the
    shared file's stale ``gpt-5.5``). An in-TUI ``/model`` later overwrites
    the same line, so user switches still win.

    :param codex_home: Private per-session ``CODEX_HOME`` directory.
    :param model: Validated model id to pin.
    """
    from omnigent.reasoning_effort import clamp_effort_for_model

    config_path = codex_home / "config.toml"
    # Same symlink-materialization dance as the MCP injection: never edit
    # the user's real config.toml through the link.
    if config_path.is_symlink():
        target = config_path.resolve()
        config_path.unlink()
        if target.is_file():
            import shutil

            shutil.copy2(target, config_path)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    pin_line = f"model = {json.dumps(model)}"
    lines = existing.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("["):
            break
        if re.match(r"^model\s*=", line):
            lines[i] = pin_line
            replaced = True
            continue
        # The config copies the user's default effort (e.g. xhigh), which the
        # pinned model may reject (GLM has no xhigh). Clamp it to a value the
        # model accepts rather than 400 the turn.
        effort_match = _EFFORT_KEY_RE.match(line)
        if effort_match:
            clamped = clamp_effort_for_model(effort_match.group(2), model)
            if clamped and clamped != effort_match.group(2):
                lines[i] = f"{effort_match.group(1)}{clamped}{effort_match.group(3)}"
    if not replaced:
        lines.insert(0, pin_line)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sync_codex_developer_instructions(
    codex_home: Path,
    instructions: str | None,
) -> None:
    """Synchronize framework instructions in the private Codex config.

    Codex's top-level ``developer_instructions`` setting is additive to its
    built-in operating instructions. The collaboration-mode field is not: a
    non-null value replaces the mode's defaults. The private session config
    therefore stores the user's original value in a sidecar, then derives the
    active value from that base on every launch. Fresh sessions append the
    framework directive; resumed sessions restore the unmodified base.

    :param codex_home: Private per-session ``CODEX_HOME`` directory.
    :param instructions: Framework instructions for this launch, or ``None``.
    :returns: None.
    """
    addition = instructions.strip() if instructions else ""
    config_path = codex_home / "config.toml"
    base_path = codex_home / ".omnigent-developer-instructions-base"
    if config_path.is_symlink():
        target = config_path.resolve()
        config_path.unlink()
        if target.is_file():
            import shutil

            shutil.copy2(target, config_path)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    try:
        document = tomlkit.parse(existing) if existing else tomlkit.document()
    except Exception:  # noqa: BLE001 - title metadata must never block Codex startup.
        _logger.warning(
            "Could not synchronize native Codex framework instructions: invalid private config",
            exc_info=True,
        )
        return
    current = document.get("developer_instructions")
    if current is not None and not isinstance(current, str):
        _logger.warning(
            "Could not synchronize native Codex framework instructions: "
            "developer_instructions is not a string"
        )
        return
    if base_path.exists():
        base = base_path.read_text(encoding="utf-8")
    else:
        base = current.strip() if isinstance(current, str) else ""
        # A previous Omnigent build may have appended the same framework
        # directive without writing the sidecar. Recover the user-authored
        # prefix instead of permanently capturing the combined value as base.
        if addition and base == addition:
            base = ""
        elif addition and base.endswith(f"\n\n{addition}"):
            base = base[: -len(addition)].rstrip()
        base_path.write_text(base, encoding="utf-8")
    active = f"{base}\n\n{addition}" if base and addition else base or addition
    if active:
        document["developer_instructions"] = active
    elif "developer_instructions" in document:
        del document["developer_instructions"]
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _codex_model_upgrade_target(catalog: object, model: str) -> str | None:
    """Return Codex's replacement for *model*, when the catalog declares one."""
    if not isinstance(catalog, dict):
        return None
    models = catalog.get("models")
    if not isinstance(models, list):
        return None
    for entry in models:
        if not isinstance(entry, dict) or entry.get("slug") != model:
            continue
        upgrade = entry.get("upgrade")
        if not isinstance(upgrade, dict):
            return None
        target = upgrade.get("model") or upgrade.get("id")
        if isinstance(target, str) and target and target != model:
            return target
        return None
    return None


def _acknowledge_codex_model_migration(codex_home: Path, model: str, target: str) -> None:
    """Suppress one model-migration prompt in a private runner-owned config."""
    config_path = codex_home / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    document = tomlkit.parse(existing) if existing else tomlkit.document()
    notice = document.get("notice")
    if notice is None:
        notice = tomlkit.table()
        document["notice"] = notice
    migrations = notice.get("model_migrations")
    if migrations is None:
        migrations = tomlkit.table()
        notice["model_migrations"] = migrations
    migrations[model] = target
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _inject_mcp_server_config(
    codex_home: Path,
    bridge_dir: Path,
    python_executable: str | None = None,
    *,
    routed_spawns: bool = False,
) -> None:
    """
    Upsert Omnigent MCP server config into ``config.toml``.

    Writes a ``[mcp_servers.omnigent]`` section that points Codex
    at the ``serve-mcp`` subprocess. This supplements the ``-c``
    overrides (which ``codex app-server`` may not honor) by writing
    directly to the config file. The write is idempotent so terminal
    relaunches do not corrupt TOML with duplicate table headers.

    :param codex_home: Private per-session CODEX_HOME directory.
    :param bridge_dir: Bridge directory containing ``bridge.json``
        and ``tool_relay.json``.
    :param python_executable: Python executable for serve-mcp.
        ``None`` uses :data:`sys.executable`.
    :param routed_spawns: ``True`` for an auto-harness Smart Routing session,
        which pre-approves the cross-harness redirect tools too.
    :returns: None.
    """
    config_path = codex_home / "config.toml"
    # Materialize the symlink from _populate_codex_home_config before
    # editing so the user's real config.toml stays untouched.
    if config_path.is_symlink():
        target = config_path.resolve()
        config_path.unlink()
        if target.is_file():
            import shutil

            shutil.copy2(target, config_path)
    if config_path.exists():
        existing = config_path.read_text(encoding="utf-8")
    else:
        existing = ""
    updated = _remove_toml_table(existing, "mcp_servers.omnigent")
    section = _codex_mcp_server_config_section(
        bridge_dir, python_executable, routed_spawns=routed_spawns
    )
    rendered = f"{updated}\n\n{section}" if updated else section
    config_path.write_text(rendered, encoding="utf-8")


class CodexAppServerResponseError(RuntimeError):
    """Structured JSON-RPC error returned by the Codex app-server."""

    def __init__(self, error: object) -> None:
        """Preserve the JSON-RPC code and message while remaining a RuntimeError."""
        self.error = error
        self.code: int | None = None
        self.message: str | None = None
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            self.code = code if isinstance(code, int) else None
            self.message = message if isinstance(message, str) else None
        super().__init__(str(error))


class CodexAppServerClient:
    """JSON-RPC client for a Codex app-server.

    Connects via Unix socket (``socket_path``) or TCP websocket
    (``ws_url``). Exactly one must be provided.

    :param socket_path: App-server Unix socket path, e.g.
        ``"/tmp/codex.sock"``.
    :param ws_url: App-server WebSocket URL, e.g.
        ``"ws://127.0.0.1:9876"``.
    :param client_name: App-server client name for the initialize
        handshake, e.g. ``"omnigent-codex-native"``.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        ws_url: str | None = None,
        client_name: str = "omnigent",
    ) -> None:
        if socket_path is None and ws_url is None:
            raise ValueError("CodexAppServerClient requires socket_path or ws_url")
        self._socket_path = socket_path
        self._ws_url = ws_url
        self._client_name = client_name
        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending_requests: dict[int, asyncio.Future[CodexMessage]] = {}
        self._events: asyncio.Queue[CodexMessage] = asyncio.Queue()
        self._next_id = 1

    async def connect(self) -> None:
        """
        Connect to the app-server and run the initialize handshake.

        :returns: None.
        """
        if self._ws_url is not None:
            self._ws = await websockets.connect(
                self._ws_url,
                max_size=_MAX_WEBSOCKET_MESSAGE_SIZE_BYTES,
                compression=None,
            )
        else:
            self._ws = await websockets.unix_connect(
                path=str(self._socket_path),
                uri=_UDS_WEBSOCKET_HANDSHAKE_URI,
                max_size=_MAX_WEBSOCKET_MESSAGE_SIZE_BYTES,
                compression=None,
            )
        self._reader_task = asyncio.create_task(
            self._reader_loop(),
            name="codex-native-app-server-reader",
        )
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self._client_name,
                    "version": "0.1",
                },
                "capabilities": {
                    "experimentalApi": True,
                },
            },
        )
        await self.notify("initialized")

    async def close(self) -> None:
        """
        Close the app-server client connection.

        :returns: None.
        """
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        if self._ws is not None:
            await self._ws.close()
        self._ws = None
        self._reader_task = None

    async def request(self, method: str, params: CodexParams) -> CodexMessage:
        """
        Send one JSON-RPC request and wait for its response.

        :param method: App-server method, e.g. ``"thread/start"``.
        :param params: JSON-serializable method parameters.
        :returns: Decoded response envelope.
        :raises CodexAppServerResponseError: If the app-server returns an error.
        """
        if self._ws is None:
            raise RuntimeError("Codex app-server client is not connected")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CodexMessage] = loop.create_future()
        self._pending_requests[request_id] = future
        await self._ws.send(
            json.dumps(
                {
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        )
        response = await future
        error = response.get("error")
        if error:
            raise CodexAppServerResponseError(error)
        return response

    async def notify(self, method: str, params: CodexParams | None = None) -> None:
        """
        Send one JSON-RPC notification.

        :param method: App-server notification method, e.g.
            ``"initialized"``.
        :param params: Optional JSON-serializable notification params.
        :returns: None.
        """
        if self._ws is None:
            raise RuntimeError("Codex app-server client is not connected")
        message: CodexMessage = {"method": method}
        if params is not None:
            message["params"] = params
        await self._ws.send(json.dumps(message))

    async def respond(self, request_id: int | str, result: CodexParams) -> None:
        """
        Send one JSON-RPC result for an app-server request.

        Codex app-server can send server-to-client requests on the
        same websocket, such as ``mcpServer/elicitation/request``.
        The forwarder handles those requests through Omnigent and replies
        with this method using Codex's original request id.

        :param request_id: JSON-RPC id from the Codex request, e.g.
            ``7`` or ``"7"``.
        :param result: JSON-serializable result payload for that
            request.
        :returns: None.
        :raises RuntimeError: If the app-server client is not
            connected.
        """
        if self._ws is None:
            raise RuntimeError("Codex app-server client is not connected")
        await self._ws.send(json.dumps({"id": request_id, "result": result}))

    async def iter_events(self) -> AsyncIterator[CodexMessage]:
        """
        Yield app-server notifications until the connection closes.

        :returns: Async iterator of notification envelopes.
        """
        while True:
            yield await self._events.get()

    async def _reader_loop(self) -> None:
        """
        Read messages from the websocket and route responses/events.

        :returns: None.
        """
        assert self._ws is not None
        async for raw in self._ws:
            if not isinstance(raw, str):
                continue
            decoded: object = json.loads(raw)
            message = _string_object_dict(decoded)
            if message is None:
                _logger.warning("Ignoring non-object Codex app-server message")
                continue
            if (
                "id" in message
                and "method" not in message
                and ("result" in message or "error" in message)
            ):
                raw_id = message["id"]
                request_id: int | None = None
                if isinstance(raw_id, int):
                    request_id = raw_id
                elif isinstance(raw_id, str):
                    with contextlib.suppress(ValueError):
                        request_id = int(raw_id)
                future = (
                    self._pending_requests.pop(request_id, None)
                    if request_id is not None
                    else None
                )
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            await self._events.put(message)


async def list_codex_model_options(client: CodexAppServerClient) -> list[_JsonObject]:
    """Read every visible model from an initialized Codex app-server client.

    :param client: Connected Codex app-server client.
    :returns: Raw ``model/list`` rows in Codex preference order.
    :raises ValueError: When Codex returns a malformed response.
    """
    options: list[_JsonObject] = []
    cursor: str | None = None
    while True:
        params: CodexParams = {"includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.request("model/list", params)
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("Codex model/list result must be an object")
        data = result.get("data")
        if not isinstance(data, list):
            raise ValueError("Codex model/list data must be a list")
        for raw_model in data:
            if not isinstance(raw_model, dict):
                raise ValueError("Codex model/list item must be an object")
            options.append(raw_model)
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            return options
        if not isinstance(next_cursor, str) or not next_cursor:
            raise ValueError("Codex model/list nextCursor must be a string or null")
        cursor = next_cursor


_model_discovery_cache: TTLCache[str, tuple[_JsonObject, ...]] = TTLCache(
    maxsize=8,
    ttl=_MODEL_DISCOVERY_CACHE_SECONDS,
)


async def discover_codex_model_options(*, codex_path: str | None = None) -> list[_JsonObject]:
    """Query the installed Codex CLI's credential-free compatibility catalog.

    Starts a short-lived app-server with an empty private ``CODEX_HOME`` and
    no provider overrides. The resulting ``model/list`` is Codex's own curated
    compatibility set; callers can intersect it with a provider's live
    availability without exposing provider credentials to the subprocess.

    :param codex_path: Optional Codex executable override.
    :returns: Raw visible ``model/list`` rows in Codex preference order.
    :raises ImportError: When the Codex CLI is unavailable.
    :raises RuntimeError: When the discovery app-server exits before connecting.
    :raises TimeoutError: When the discovery app-server does not become ready.
    """
    resolved_codex = codex_path or _find_codex_cli()
    if not resolved_codex:
        raise ImportError("Native Codex model discovery requires the 'codex' CLI on PATH.")
    cached = _model_discovery_cache.get(resolved_codex)
    if cached is not None:
        return [dict(option) for option in cached]

    with tempfile.TemporaryDirectory(prefix="omnigent-codex-model-discovery-") as raw_dir:
        root = Path(raw_dir)
        codex_home = root / "codex-home"
        codex_home.mkdir(mode=0o700)
        port = _allocate_loopback_port()
        listen_url = f"ws://127.0.0.1:{port}"
        env = _clean_codex_env()
        for name in tuple(env):
            if name.startswith("OPENAI_") or name in {
                "DATABRICKS_BEARER",
                "DATABRICKS_CODEX_TOKEN",
            }:
                env.pop(name)
        env["CODEX_HOME"] = str(codex_home)
        process = await _start_codex_model_discovery_process(
            codex_path=resolved_codex,
            listen_url=listen_url,
            env=env,
            cwd=root,
        )
        client: CodexAppServerClient | None = None
        try:
            await _wait_for_discovery_listener(process, port)
            client = CodexAppServerClient(
                ws_url=listen_url,
                client_name="omnigent-codex-model-discovery",
            )
            await client.connect()
            options = await list_codex_model_options(client)
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            _proc.terminate_tree(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                _proc.kill_tree(process)
                await process.wait()

    _model_discovery_cache[resolved_codex] = tuple(dict(option) for option in options)
    return options


async def _start_codex_model_discovery_process(
    *,
    codex_path: str,
    listen_url: str,
    env: dict[str, str],
    cwd: Path,
    config_overrides: Sequence[str] = (),
) -> asyncio.subprocess.Process:
    """Start the isolated Codex process used only for model discovery."""
    override_args: list[str] = []
    for override in config_overrides:
        override_args.extend(("-c", override))
    return await asyncio.create_subprocess_exec(
        codex_path,
        "app-server",
        "--listen",
        listen_url,
        *override_args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=str(cwd),
        **_proc.spawn_kwargs(),
    )


def _allocate_loopback_port() -> int:
    """Return an ephemeral TCP port on loopback for model discovery."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_for_discovery_listener(
    process: asyncio.subprocess.Process,
    port: int,
) -> None:
    """Wait until a discovery app-server accepts loopback connections."""
    deadline = asyncio.get_running_loop().time() + _CONNECT_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"Codex model discovery exited early ({process.returncode})")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)
            continue
        writer.close()
        await writer.wait_closed()
        del reader
        return
    raise TimeoutError("Timed out waiting for Codex model discovery app-server")


def _probe_codex_home(config_overrides: Sequence[str]) -> Path:
    """
    Persistent probe ``CODEX_HOME`` for one provider configuration.

    Persistent (unlike the hermetic discovery's temp dir) so Codex's own
    ``models_cache.json`` ETag handling makes repeat probes cheap; keyed by
    the override set so a provider change never replays another provider's
    cache. The account's real ``auth.json`` is symlinked in, the same way
    a session launch links it: the credential decides which models the
    account's catalog lists (login-gated entries, the account default), so
    a credential-less probe answers for a catalog no session will see.

    :param config_overrides: The probe's ``-c`` overrides.
    :returns: The created ``CODEX_HOME`` directory.
    """
    key = hashlib.sha256("\n".join(config_overrides).encode("utf-8")).hexdigest()[:12]
    home = Path.home() / ".omnigent" / "cache" / "codex-model-probe" / key
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    real_auth = _codex_home_config_source_from_env() / "auth.json"
    probe_auth = home / "auth.json"
    if real_auth.exists():
        with contextlib.suppress(OSError):
            if probe_auth.is_symlink() or probe_auth.exists():
                probe_auth.unlink()
            probe_auth.symlink_to(real_auth)
    return home


def mark_launch_default(rows: list[_JsonObject], pinned_model: str | None) -> list[_JsonObject]:
    """
    Reduce ``model/list`` rows to exactly one ``isDefault`` marker.

    The launch-pinned model wins when a row names it (either spelling);
    otherwise Codex's own first default stands. Rows are otherwise verbatim.

    Codex's own ``isDefault`` is its built-in preference, which says nothing
    about the model this session launched on, so a picker that trusted it
    would name a model the pane is not running.

    :param rows: Raw ``model/list`` rows.
    :param pinned_model: The model the session runs, or ``None``.
    :returns: The rows with a single default marked.
    """
    from omnigent.codex_model_vocabulary import comparable_model_id

    codex_default_index: int | None = None
    pinned_index: int | None = None
    pinned_key = comparable_model_id(pinned_model) if pinned_model else None
    marked: list[_JsonObject] = []
    for index, row in enumerate(rows):
        cleaned = {key: value for key, value in row.items() if key != "isDefault"}
        marked.append(cleaned)
        if codex_default_index is None and row.get("isDefault") is True:
            codex_default_index = index
        if pinned_index is None and pinned_key is not None:
            for spelling in (row.get("id"), row.get("model")):
                if isinstance(spelling, str) and comparable_model_id(spelling) == pinned_key:
                    pinned_index = index
                    break
    default_index = pinned_index if pinned_index is not None else codex_default_index
    if default_index is not None:
        marked[default_index]["isDefault"] = True
    return marked


async def probe_codex_model_options(*, codex_path: str | None = None) -> list[_JsonObject]:
    """
    Ask a session-configured Codex app-server for its own model list.

    The harness is the source of truth for what a session's ``/model``
    picker would offer, so the probe boots ``codex app-server`` with the
    SAME materialization a session launch gets — for every launch shape.
    A Databricks profile contributes its provider overrides (gateway base
    URL + minted auth + model pin) and ``DATABRICKS_HOST``; other provider
    shapes carry their resolved ``-c`` overrides verbatim; the plain
    Codex-login shape probes bare, which yields the ACCOUNT's visible
    catalog: the probe home is isolated (never the user's real
    ``~/.codex``) but links the real ``auth.json`` in the way a session
    launch does, so login-gated entries and the account default match what
    a live session will offer.

    :param codex_path: Optional Codex executable override.
    :returns: The probe rows with a single default marked.
    :raises ImportError: When the Codex CLI is unavailable.
    :raises OSError: When a Databricks profile resolves no workspace host.
    :raises RuntimeError: When the probe app-server exits before connecting.
    :raises TimeoutError: When the probe app-server does not become ready.
    """
    launch = await asyncio.to_thread(resolve_native_codex_launch, model=None)
    resolved_codex = codex_path or _find_codex_cli()
    if not resolved_codex:
        raise ImportError("Native Codex model probing requires the 'codex' CLI on PATH.")
    config_overrides = list(launch.config_overrides)
    pinned_model = launch.model
    env = _clean_codex_env()
    if launch.profile is not None:
        databricks = await asyncio.to_thread(
            _databricks_launch_materialization, model=launch.model, profile=launch.profile
        )
        config_overrides.extend(databricks.config_overrides)
        env["DATABRICKS_HOST"] = databricks.host
        pinned_model = databricks.model
    codex_home = await asyncio.to_thread(_probe_codex_home, config_overrides)
    env["CODEX_HOME"] = str(codex_home)
    port = _allocate_loopback_port()
    listen_url = f"ws://127.0.0.1:{port}"
    process = await _start_codex_model_discovery_process(
        codex_path=resolved_codex,
        listen_url=listen_url,
        env=env,
        cwd=codex_home,
        config_overrides=config_overrides,
    )
    client: CodexAppServerClient | None = None
    try:
        await _wait_for_discovery_listener(process, port)
        client = CodexAppServerClient(
            ws_url=listen_url,
            client_name="omnigent-codex-model-probe",
        )
        await client.connect()
        rows = await list_codex_model_options(client)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        _proc.terminate_tree(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            _proc.kill_tree(process)
            await process.wait()
    return mark_launch_default(rows, pinned_model)


def codex_catalog_fingerprint(launch: NativeCodexLaunch) -> str:
    """The launch-config fingerprint keying codex's shared model catalog.

    One formula for every consumer (host boot probe, runner launch, live
    write-back), so they read and write the same catalog file. Callers
    fingerprint the SHAPE — a ``model=None`` resolution — so per-session
    picks never fragment the catalog.

    :param launch: The resolved launch (``resolve_native_codex_launch``).
    :returns: A stable fingerprint string.
    """
    from omnigent.model_catalog_store import fingerprint_of

    return fingerprint_of(
        "codex-native", launch.profile, launch.model, tuple(launch.config_overrides)
    )


async def codex_launch_catalog(*, codex_path: str | None = None) -> list[_JsonObject] | None:
    """
    The shared codex catalog for this host's default shape: store, then probe.

    Reads the on-disk catalog for the ``model=None`` launch shape; a miss
    pays one session-shaped probe (real auth linked in) and persists the
    answer for every later consumer.

    :param codex_path: Optional Codex executable override.
    :returns: Catalog rows, or ``None`` when no catalog could be obtained.
    """
    from omnigent import model_catalog_store

    try:
        launch = await asyncio.to_thread(resolve_native_codex_launch, model=None)
    except Exception:  # noqa: BLE001 — a broken provider config means no catalog
        _logger.warning("codex catalog: launch shape resolution failed", exc_info=True)
        return None
    fingerprint = codex_catalog_fingerprint(launch)

    async def _probe() -> list[_JsonObject] | None:
        try:
            return await probe_codex_model_options(codex_path=codex_path)
        except Exception:  # noqa: BLE001 — probe failure means "no catalog", never a crash
            _logger.warning("codex catalog probe failed", exc_info=True)
            return None

    return await model_catalog_store.ensure_catalog("codex-native", fingerprint, _probe)


async def codex_launch_catalog_is_stale() -> bool:
    """
    Whether the default launch shape's stored catalog is past the TTL.

    :returns: ``True`` when the store holds only a stale entry; ``False``
        when it is fresh, absent, or the launch shape cannot resolve.
    """
    from omnigent import model_catalog_store

    try:
        launch = await asyncio.to_thread(resolve_native_codex_launch, model=None)
    except Exception:  # noqa: BLE001 — a broken provider config means no catalog
        return False
    return model_catalog_store.catalog_is_stale("codex-native", codex_catalog_fingerprint(launch))


def _build_native_codex_app_server_argv(
    *,
    tagged_argv0: str,
    listen_url: str,
    config_overrides: Sequence[str],
) -> list[str]:
    """Build argv for the native Codex app-server subprocess."""
    argv = [tagged_argv0, "app-server", "--listen", listen_url]
    for override in config_overrides:
        argv.extend(["-c", override])
    return argv


@dataclass
class CodexNativeAppServer:
    """
    Running native Codex app-server subprocess.

    :param codex_path: Executable path for the Codex CLI, e.g.
        ``"/usr/local/bin/codex"``.
    :param socket_path: Codex app-server Unix socket path.
    :param codex_home: Private per-session ``CODEX_HOME`` path.
    :param env: Environment for the app-server subprocess.
    :param config_overrides: Codex ``-c`` config override values.
    :param developer_instructions: Optional framework-owned instructions
        appended to the private session config before app-server startup.
    :param cwd: Working directory for the app-server process.
    :param bridge_dir: Native Codex bridge directory, e.g.
        ``Path("~/.omnigent/codex-native/<hash>")``. The policy hook
        subprocess is pointed at it via ``--bridge-dir`` and reads the
        session id + Omnigent coordinates from it.
    :param ap_server_url: Omnigent server base URL the policy hook POSTs tool
        calls to, e.g. ``"http://127.0.0.1:8787"``. ``None`` registers
        and trusts the hook but writes no Omnigent coordinates, so the hook
        no-ops (no enforcement) until coordinates exist.
    :param ap_auth_headers: Outbound auth headers for the policy hook's
        Omnigent requests, e.g. ``{"Authorization": "Bearer <token>"}``.
    :param python_executable: Python executable the policy hook command
        runs, e.g. ``"/path/to/.venv/bin/python"``. ``None`` uses
        :data:`sys.executable`.
    :param policy_hook_disabled_reason: Runtime field set by
        :meth:`start`: ``None`` when the tool-call policy hook is active
        (registered + trusted), or a human-readable reason string when it
        is NOT enforced for this session — codex too old, or the trust
        handshake failed. Hook problems are non-fatal (fail-open): the
        session still starts and this reason is surfaced as a web-UI
        notice rather than blocking session creation. Not a constructor
        input — defaults ``None`` until ``start``.
    :param pinned_model: Session-pinned model id written into the
        per-session ``config.toml`` at start, or ``None``. Keeps the
        forwarder's config.toml model mirror (and the cost gate's hook
        read) consistent with what the session was launched to run.
    :param trust_project: Whether to trust :attr:`cwd` in the private
        session config before startup. Runner-owned headless sessions set
        this because nobody can answer Codex's project-trust TUI prompt.
        Interactive CLI sessions leave it disabled.
    :param policy_notice_pending: One-shot flag: ``True`` once a degrade
        reason is recorded, until the runner's terminal-ensure handler
        surfaces it to Omnigent (which posts a single durable banner). Prevents
        re-posting the same notice on every subsequent ensure. Not a
        constructor input.
    """

    codex_path: str
    socket_path: Path
    codex_home: Path
    env: dict[str, str]
    config_overrides: list[str]
    cwd: Path
    bridge_dir: Path
    developer_instructions: str | None = None
    ap_server_url: str | None = None
    ap_auth_headers: dict[str, str] | None = None
    python_executable: str | None = None
    listen_url: str | None = None
    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    recent_stderr: list[str] | None = None
    policy_hook_disabled_reason: str | None = None
    policy_notice_pending: bool = False
    pinned_model: str | None = None
    process_registry_tag: str | None = None
    process_owner_lock: CodexNativeProcessOwnerLock | None = None
    codex_cli_version: tuple[int, int, int] | None = None
    trust_project: bool = False
    router_hooks_registered: bool = False

    async def start(self) -> None:
        """
        Start the Codex app-server and wait for the socket.

        :returns: None.
        """
        self.codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.codex_home, 0o700)
        if self.listen_url is None or self.listen_url.startswith("unix://"):
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()
        # Native policy enforcement needs codex's hook-trust protocol
        # (``currentHash`` / ``trustStatus`` in ``hooks/list``), added in
        # codex 0.129. Below that the hook can never be trusted, so
        # registering it would only fail at the trust gate. Probed before
        # the home is populated: on an unsupported codex no hooks file is
        # generated at all, so the user's hooks.json must still be
        # symlinked in rather than left missing. A version we cannot parse
        # (``None``) is treated as supported so a flaky probe never
        # silently disables enforcement — a genuine trust failure is then
        # caught below.
        codex_version = await _codex_cli_version(self.codex_path)
        self.codex_cli_version = codex_version
        policy_hooks_supported = (
            codex_version is None or codex_version >= _MIN_POLICY_HOOK_CODEX_VERSION
        )
        # When the runner advertises a route-subagent endpoint, the generated
        # hooks file owns hooks.json, so the user's copy is merged in rather
        # than symlinked over. The runner advertises it for auto-harness Smart
        # Routing sessions only, so its presence is also this session class's
        # signature — see ``ensure_session_router_quietly``.
        router_bridge_dir = codex_router_bridge_dir(self.env)
        if router_bridge_dir is not None:
            # A CLI too old for the spawn gate gets no routing hooks at all, so
            # routing no-ops instead of blocking the launch. Everything keyed
            # off the advertisement below (generated hooks.json, the routed-spawn
            # tool pre-approvals) then falls back to the plain shape.
            skip_reason = codex_routing_hook_skip_reason(codex_version)
            if skip_reason is not None:
                _logger.warning("%s", skip_reason)
                router_bridge_dir = None
        self.router_hooks_registered = router_bridge_dir is not None and policy_hooks_supported
        routed_spawns = router_bridge_dir is not None
        config_source = _codex_home_config_source_from_env()
        model_migration_target: str | None = None
        if self.trust_project and self.pinned_model:
            catalog = await asyncio.to_thread(
                read_codex_model_catalog,
                self.codex_path,
                config_source,
                timeout=_MODEL_MIGRATION_CATALOG_TIMEOUT_SECONDS,
            )
            model_migration_target = _codex_model_upgrade_target(catalog, self.pinned_model)
        # Off the loop: this copies/symlinks a home AND (on a Smart Routing
        # session) shells out to ``codex debug models`` with a 10s timeout. Run
        # inline it stalled every other session sharing this event loop for that
        # long — which is also why a plain session must never reach the probe.
        await asyncio.to_thread(
            _populate_codex_home_config,
            self.codex_home,
            config_source,
            inject_hooks=self.router_hooks_registered,
            extend_model_catalog=codex_extended_catalog_requested(self.env),
        )
        if self.trust_project:
            _trust_codex_project(self.codex_home, self.cwd)
        # Write the MCP server config into config.toml so the app-server
        # discovers it at config load. The -c overrides may not be honored
        # by `codex app-server`, so we write directly to the file.
        _inject_mcp_server_config(
            self.codex_home,
            self.bridge_dir,
            self.python_executable,
            routed_spawns=routed_spawns,
        )
        if self.pinned_model:
            _pin_codex_config_model(self.codex_home, self.pinned_model)
            if model_migration_target is not None:
                _acknowledge_codex_model_migration(
                    self.codex_home,
                    self.pinned_model,
                    model_migration_target,
                )
        _sync_codex_developer_instructions(
            self.codex_home,
            self.developer_instructions,
        )
        self.config_overrides = materialize_codex_provider_config(
            self.codex_home,
            self.config_overrides,
        )
        if codex_version is not None and not policy_hooks_supported:
            self._disable_policy_hook(
                f"Codex CLI {_format_codex_version(codex_version)} is older than "
                f"{_format_codex_version(_MIN_POLICY_HOOK_CODEX_VERSION)}; upgrade "
                "codex to enforce tool-call policies."
            )
        else:
            # Register the Omnigent policy hook in this private CODEX_HOME
            # *before* launching the app-server so codex discovers it at
            # config load. The Omnigent coordinates the hook subprocess
            # needs go in the bridge dir's policy_hook.json; without
            # ap_server_url the hook is still registered + trusted but
            # no-ops.
            _write_codex_policy_hooks_file(
                self.codex_home,
                self.bridge_dir,
                self.python_executable,
                router_bridge_dir=router_bridge_dir,
                router_session_id=codex_router_session_id(self.env),
                user_hooks_source=config_source / _CODEX_HOOKS_FILE,
                # The runner only advertises a route-turn endpoint for a
                # session that launched with Smart Routing on, so its presence
                # is the switch for the first-message routing hook. Same
                # rendezvous-as-switch shape as the subagent router above.
                turn_routing=_turn_router_advertised(self.bridge_dir),
            )
            if self.ap_server_url:
                write_policy_hook_config(
                    self.bridge_dir,
                    ap_server_url=self.ap_server_url,
                    ap_auth_headers=self.ap_auth_headers or {},
                )
        reconcile_codex_native_process_registry()
        resolved_listen = self.listen_url or f"unix://{self.socket_path}"
        self.process_registry_tag = f"codex-native-{uuid.uuid4().hex}"
        tagged_argv0 = (
            f"{Path(self.codex_path).name} "
            f"{codex_native_session_tag_cmdline_arg(self.process_registry_tag)}"
        )
        argv = _build_native_codex_app_server_argv(
            tagged_argv0=tagged_argv0,
            listen_url=resolved_listen,
            config_overrides=self.config_overrides,
        )
        proc_env = {**self.env, "CODEX_HOME": str(self.codex_home)}
        self.process_owner_lock = acquire_codex_native_process_owner_lock()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
                cwd=str(self.cwd),
                executable=self.codex_path,
                **_proc.spawn_kwargs(),
            )
        except BaseException:
            if self.process_owner_lock is not None:
                self.process_owner_lock.close()
                self.process_owner_lock = None
            raise
        if self.process_owner_lock is not None:
            register_codex_native_process(
                pid=self.proc.pid,
                pgid=_process_group_id(self.proc),
                session_tag=self.process_registry_tag,
                owner_lock_path=self.process_owner_lock.path,
            )
        self.recent_stderr = []
        self.stderr_task = asyncio.create_task(
            self._stderr_loop(),
            name="codex-native-app-server-stderr",
        )
        # Ordering invariant: hooks.json is written before the spawn above,
        # and the trust handshake must complete before the first turn — codex
        # resolves trust when it dispatches a hook, so trust landing after the
        # spawn is fine, but a turn started before it runs unhooked. The
        # handshake cannot precede the spawn (``hooks/list`` is an app-server
        # RPC), so callers must not launch the TUI or dispatch a turn until
        # ``start()`` returns.
        # Readiness failure (the app-server never came up) is fatal and
        # tears down the subprocess so it is not orphaned. Policy-hook
        # trust, by contrast, is best-effort: a trust failure degrades the
        # session to "no enforcement" with a surfaced reason rather than
        # blocking session creation (fail-open). ``BaseException`` on the
        # outer guard so a cancellation mid-trust still tears down.
        try:
            await self._wait_until_ready()
            if self.policy_hook_disabled_reason is None:
                try:
                    await self._trust_policy_hooks()
                except Exception as exc:  # noqa: BLE001 - degrade, never block startup
                    self._disable_policy_hook(f"Codex policy hook could not be trusted: {exc}")
        except BaseException:
            await self.close()
            raise

    async def _trust_policy_hooks(self) -> None:
        """
        Mark the registered Omnigent policy hook as trusted.

        A freshly-written non-managed hook is ``untrusted`` and codex
        silently skips untrusted hooks — for a policy gate that is a
        fail-open. This connects a transient app-server client and runs
        the same ``hooks/list`` → ``config/batchWrite`` trust flow codex's
        own TUI uses, then verifies the hook is trusted. ``--listen`` may
        be a unix socket (local CLI) or a loopback websocket (host
        runner); both transports are handled.

        :returns: None.
        :raises RuntimeError: If the policy hook is missing from the
            discovered set or remains untrusted after the trust write —
            either condition means enforcement would silently not run.
            The message is augmented with codex's captured configuration
            error (see :meth:`_codex_config_error_hint`) when present.
        """
        if self.listen_url and self.listen_url.startswith("ws://"):
            client = CodexAppServerClient(
                ws_url=self.listen_url,
                client_name="omnigent-policy-trust",
            )
        else:
            client = CodexAppServerClient(
                self.socket_path,
                client_name="omnigent-policy-trust",
            )
        await client.connect()
        try:
            await trust_native_policy_hooks(client, cwd=str(self.cwd))
            # Routing hooks live in the same generated file but under a
            # different module, so they need their own trust pass. Best
            # effort: a routing-trust failure must not disable the policy
            # gate, so it is logged instead of raised.
            if self.router_hooks_registered:
                try:
                    await trust_codex_router_hooks(client.request, cwd=str(self.cwd))
                except Exception:  # noqa: BLE001 - routing trust never blocks startup
                    _logger.warning(
                        "codex subagent-routing hook trust failed; routing will not be enforced",
                        exc_info=True,
                    )
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}{self._codex_config_error_hint()}") from exc
        finally:
            await client.close()

    def _codex_config_error_hint(self) -> str:
        """
        Return a hint drawn from codex stderr when policy-hook trust fails.

        Codex logs ``Invalid configuration; using defaults`` when the
        per-session ``config.toml`` is not valid TOML, then loads zero
        hooks — the most common cause of a "not discovered" failure.
        Surfacing the captured stderr line (which the trust handshake
        otherwise never sees) turns an opaque failure into a
        self-diagnosing error pointing at the offending config file.

        :returns: A space-prefixed hint string, or ``""`` when codex
            reported no configuration problem.
        """
        config_errors = [
            line for line in (self.recent_stderr or []) if "Invalid configuration" in line
        ]
        if not config_errors:
            return ""
        joined = " | ".join(config_errors[-3:])
        return (
            f" Codex reported a configuration problem (the per-session config "
            f"{self.codex_home / 'config.toml'} is likely invalid TOML, so codex "
            f"loaded no hooks): {joined}"
        )

    def _disable_policy_hook(self, reason: str) -> None:
        """
        Record that tool-call policy enforcement is inactive (fail-open).

        Single entry point used by :meth:`start` for both degrade paths
        (codex too old, trust handshake failed). Stores the reason, flags
        a one-shot web-UI notice for the runner's ensure handler to
        surface, and logs (see :meth:`_warn_policy_hook_disabled`).

        :param reason: Human-readable cause, e.g. ``"Codex CLI 0.128.0 is
            older than 0.129.0; upgrade codex to enforce tool-call
            policies."``.
        :returns: None.
        """
        self.policy_hook_disabled_reason = reason
        self.policy_notice_pending = True
        self._warn_policy_hook_disabled()

    def _warn_policy_hook_disabled(self) -> None:
        """
        Log that tool-call policy enforcement is inactive for this session.

        Called by :meth:`start` when it degrades the session to "no
        enforcement" — either codex is too old to trust the hook, or the
        trust handshake failed. The reason is in
        :attr:`policy_hook_disabled_reason`. When Omnigent coordinates are
        present (``ap_server_url`` set) enforcement was intended, so this
        is a loud ``warning``; otherwise nothing would have been enforced
        anyway and it is an ``info``.

        :returns: None.
        """
        message = "Native tool-call policy enforcement is NOT active for this session: %s"
        if self.ap_server_url:
            _logger.warning(message, self.policy_hook_disabled_reason)
        else:
            _logger.info(message, self.policy_hook_disabled_reason)

    async def close(self) -> None:
        """
        Stop the app-server subprocess.

        :returns: None.
        """
        if self.proc is not None and self.proc.returncode is None:
            _terminate_process_tree(self.proc)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _kill_process_tree(self.proc)
                await self.proc.wait()
        if self.process_registry_tag is not None:
            unregister_codex_native_process(self.process_registry_tag)
        if self.process_owner_lock is not None:
            self.process_owner_lock.close()
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.stderr_task
        self.proc = None
        self.stderr_task = None
        self.process_registry_tag = None
        self.process_owner_lock = None

    async def _wait_until_ready(self) -> None:
        """
        Wait until the app-server socket accepts an initialized
        client.

        :returns: None.
        :raises RuntimeError: If the app-server exits or never
            becomes ready before the timeout.
        """
        deadline = asyncio.get_running_loop().time() + _CONNECT_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self.proc is not None and self.proc.returncode is not None:
                detail = " | ".join((self.recent_stderr or [])[-5:])
                raise RuntimeError(f"Codex app-server exited early: {detail}")
            try:
                if self.listen_url and self.listen_url.startswith("ws://"):
                    client = CodexAppServerClient(
                        ws_url=self.listen_url,
                        client_name="omnigent-probe",
                    )
                else:
                    client = CodexAppServerClient(
                        self.socket_path,
                        client_name="omnigent-probe",
                    )
                await client.connect()
                await client.close()
                return
            except Exception as exc:  # noqa: BLE001 - readiness retry boundary
                last_error = exc
                await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)
        detail = " | ".join((self.recent_stderr or [])[-5:])
        raise RuntimeError(
            f"Timed out waiting for Codex app-server socket {self.socket_path}: "
            f"{last_error}; stderr={detail}"
        )

    async def _stderr_loop(self) -> None:
        """
        Capture recent app-server stderr for diagnostics.

        :returns: None.
        """
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if len(text) >= _STDERR_CHUNK_LIMIT:
                text = f"{text[:_STDERR_CHUNK_LIMIT]}...[truncated]"
            if self.recent_stderr is not None:
                self.recent_stderr.append(text)
                if len(self.recent_stderr) > 20:
                    self.recent_stderr.pop(0)
            _logger.debug("codex-native app-server stderr: %s", text)


def _codex_policy_hook_command(bridge_dir: Path, python_executable: str | None) -> str:
    """
    Build the shell command codex runs for the policy hook.

    Runs python in isolated mode (``-I``): codex executes hooks with the
    session's workspace as cwd, and ``-m`` would otherwise put that
    workspace first on ``sys.path``. A workspace holding a directory named
    like one of our packages (the omnigent checkout itself, most obviously)
    then shadows the installed one and the hook dies on an import error
    that codex discards — a silent fail-open. Mirrors the ``-I`` the
    bridge's MCP server command already uses.

    :param bridge_dir: Native Codex bridge directory passed to the hook
        via ``--bridge-dir``.
    :param python_executable: Python executable to run, e.g.
        ``"/path/to/python"``. ``None`` uses :data:`sys.executable`.
    :returns: A shell-escaped command string, e.g.
        ``"/path/python -I -m omnigent.codex_native_hook evaluate-policy
        --bridge-dir /home/u/.omnigent/codex-native/abc"``.
    """
    python = python_executable or sys.executable
    return shlex.join(
        [
            python,
            "-I",
            "-m",
            _POLICY_HOOK_MODULE,
            "evaluate-policy",
            "--bridge-dir",
            str(bridge_dir),
        ]
    )


def _turn_router_advertised(bridge_dir: Path) -> bool:
    """
    Report whether the runner advertised a ``route-turn`` endpoint here.

    :param bridge_dir: Native Codex bridge directory.
    :returns: ``True`` when a usable ``turn_router.json`` is present, i.e. the
        session launched with Smart Routing on.
    """
    from omnigent.inner.hook_scripts.subagent_router import read_router_endpoint
    from omnigent.runner.turn_routing import ADVERTISEMENT_FILE

    return read_router_endpoint(bridge_dir, filename=ADVERTISEMENT_FILE) is not None


def _codex_policy_hooks_settings(
    bridge_dir: Path,
    python_executable: str | None,
    *,
    turn_routing: bool = False,
) -> _JsonObject:
    """
    Build the ``hooks.json`` payload registering the policy hook.

    Registers one catch-all (no ``matcher``) command hook on
    ``PreToolUse`` (blocks before execution), ``PostToolUse`` (warns
    after), and ``UserPromptSubmit`` (blocks a user prompt before the
    model sees it — the request-phase gate for native sessions, since
    the server-level ``_evaluate_input_policy`` skips native message
    events). ``mcp__*`` tools are filtered out inside the hook itself,
    not by a matcher, so the relay path remains the single MCP
    enforcement point.

    :param bridge_dir: Native Codex bridge directory.
    :param python_executable: Python executable for the hook command.
    :param turn_routing: ``True`` when the runner advertised a ``route-turn``
        endpoint for this session, i.e. it launched with Smart Routing on.
        ``False`` leaves the first-message routing hook unregistered, so a
        session that will never route pays no per-prompt round trip.
    :returns: A ``hooks.json``-shaped dict.
    """
    hook: _JsonObject = {
        "type": "command",
        "command": _codex_policy_hook_command(bridge_dir, python_executable),
        "timeout": _POLICY_HOOK_TIMEOUT_SECONDS,
    }
    prompt_submit: list[_JsonObject] = [hook]
    if turn_routing:
        prompt_submit.append(_codex_route_turn_hook(bridge_dir, python_executable))
    return {
        "hooks": {
            "PreToolUse": [{"hooks": [hook]}],
            "PostToolUse": [{"hooks": [hook]}],
            "UserPromptSubmit": [{"hooks": prompt_submit}],
        }
    }


def _codex_route_turn_hook(bridge_dir: Path, python_executable: str | None) -> _JsonObject:
    """
    Build the ``UserPromptSubmit`` entry for first-message model routing.

    A second command alongside the policy gate rather than a module of its
    own: codex trusts hooks by command, and the trust pass filters on
    :data:`_POLICY_HOOK_MODULE`, so keeping the subcommand there rides the
    existing handshake. It no-ops (exit 0, no output) unless the runner has
    advertised a ``route-turn`` endpoint and nothing has pinned the
    session's model yet; when it does route, it blocks the prompt and the
    runner replays it on the routed model. See
    :mod:`omnigent.runner.turn_routing`.

    :param bridge_dir: Native Codex bridge directory, holding both the
        endpoint advertisement and the marker file.
    :param python_executable: Python executable for the hook command.
    :returns: One ``hooks.json`` command-hook entry.
    """
    from omnigent.runner.turn_routing import HARNESS_HOOK_TIMEOUT_S

    return {
        "type": "command",
        "command": shlex.join(
            [
                python_executable or sys.executable,
                "-I",
                "-m",
                _POLICY_HOOK_MODULE,
                "route-turn",
                "--bridge-dir",
                str(bridge_dir),
                "--harness",
                "codex-native",
            ]
        ),
        "timeout": HARNESS_HOOK_TIMEOUT_S,
    }


def _write_codex_policy_hooks_file(
    codex_home: Path,
    bridge_dir: Path,
    python_executable: str | None,
    *,
    router_bridge_dir: Path | None = None,
    router_session_id: str | None = None,
    user_hooks_source: Path | None = None,
    turn_routing: bool = False,
) -> None:
    """
    Write ``hooks.json`` into the private CODEX_HOME (atomically).

    This file is the only ``hooks.json`` codex loads, so the policy hooks,
    the subagent-routing hooks and the user's own hooks all go through the
    shared :func:`write_codex_hooks_file` into one payload — written
    separately, whichever ran last would erase the other.

    :param codex_home: Private per-session ``CODEX_HOME`` directory.
    :param bridge_dir: Native Codex bridge directory for the hook command.
    :param python_executable: Python executable for the hook command.
    :param router_bridge_dir: Directory advertising the route-subagent
        endpoint. ``None`` leaves native subagent spawns unrouted.
    :param router_session_id: Session id baked into the routing hook
        commands.
    :param user_hooks_source: The user's real ``hooks.json`` to merge when
        the private home holds no symlink to it (the routing path unlinks
        it before this runs).
    :param turn_routing: ``True`` when the session launched with Smart Routing
        on, which registers the ``UserPromptSubmit`` first-message routing
        hook.
    :returns: None.
    """
    payloads: list[Mapping[str, object]] = [
        _codex_policy_hooks_settings(bridge_dir, python_executable, turn_routing=turn_routing)
    ]
    if router_bridge_dir is not None:
        payloads.append(
            codex_router_hooks_settings(
                router_bridge_dir,
                session_id=router_session_id,
                harness="codex-native",
                python_executable=python_executable,
            )
        )
    _ = write_codex_hooks_file(codex_home, payloads, user_hooks_source=user_hooks_source)


def _our_hooks_from_list(listed: _JsonObject, cwd: str, module: str) -> list[_JsonObject]:
    """
    Extract the hooks for *cwd* whose command runs *module*.

    Filtering by module keeps the trust step from ever touching hooks the
    user's own ``hooks.json`` contributed to the merged file.

    :param listed: Parsed ``hooks/list`` response envelope, with
        ``result.data`` a list of ``{cwd, hooks: [...]}`` entries.
    :param cwd: The cwd whose hook set to read, e.g.
        ``"/home/user/repo"``.
    :param module: Hook-script module marker, e.g.
        ``"omnigent.codex_native_hook"``.
    :returns: The matching hook metadata dicts (possibly empty), each
        with ``key``, ``currentHash``, ``trustStatus``.
    """
    result = _string_object_dict(listed.get("result"))
    if result is None:
        result = listed
    data = _object_list(result.get("data")) or []
    for raw_entry in data:
        entry = _string_object_dict(raw_entry)
        if entry is not None and entry.get("cwd") == cwd:
            hooks = _object_list(entry.get("hooks")) or []
            return [
                hook
                for raw_hook in hooks
                if (hook := _string_object_dict(raw_hook)) is not None
                and module in str(hook.get("command", ""))
            ]
    return []


def _our_policy_hooks_from_list(listed: _JsonObject, cwd: str) -> list[_JsonObject]:
    """
    Extract *our* policy hooks for *cwd* from a ``hooks/list`` response.

    :param listed: Parsed ``hooks/list`` response envelope.
    :param cwd: The cwd whose hook set to read, e.g.
        ``"/home/user/repo"``.
    :returns: The matching Omnigent policy-hook metadata dicts.
    """
    return _our_hooks_from_list(listed, cwd, _POLICY_HOOK_MODULE)


def _hooks_list_diagnostics(listed: _JsonObject, cwd: str) -> str:
    """
    Summarize a ``hooks/list`` response for a discovery-failure error.

    Turns an opaque "hook not discovered" into a self-diagnosing message
    by reporting what codex actually returned. Distinguishes the common
    causes:

    - **zero entries / zero hooks** — codex loaded no hooks at all,
      typically because the per-session ``config.toml`` is invalid TOML
      and codex fell back to defaults;
    - **cwd mismatch** — entries came back but none for the queried
      *cwd*;
    - **module mismatch** — an entry matched but no hook command
      references :data:`_POLICY_HOOK_MODULE` (e.g. a stale / renamed
      module from an out-of-date install).

    :param listed: Parsed ``hooks/list`` response envelope.
    :param cwd: The cwd that was queried, e.g. ``"/home/user/repo"``.
    :returns: A one-line diagnostic, e.g.
        ``"hooks/list returned no hooks (codex loaded none — likely an "
        "invalid per-session config.toml)"``.
    """
    result = _string_object_dict(listed.get("result"))
    if result is None:
        result = listed
    data = _object_list(result.get("data")) or []
    entries = [entry for raw in data if (entry := _string_object_dict(raw)) is not None]
    if not entries or all(not (_object_list(entry.get("hooks")) or []) for entry in entries):
        return (
            "hooks/list returned no hooks (codex loaded none — likely an "
            "invalid per-session config.toml, so codex fell back to defaults)"
        )
    matched_cwd = any(e.get("cwd") == cwd for e in entries)
    parts: list[str] = []
    for entry in entries:
        hooks = _object_list(entry.get("hooks")) or []
        ours = sum(
            1
            for raw_hook in hooks
            if (hook := _string_object_dict(raw_hook)) is not None
            and _POLICY_HOOK_MODULE in str(hook.get("command", ""))
        )
        parts.append(f"cwd={entry.get('cwd')!r}: {len(hooks)} hook(s), {ours} ours")
    prefix = "" if matched_cwd else f"no entry matched queried cwd {cwd!r}; "
    return f"hooks/list returned [{prefix}{'; '.join(parts)}]"


def _untrusted_hook_detail(hooks: Sequence[_JsonObject]) -> str:
    """
    Render untrusted hook metadata for a trust-failure error.

    Surfaces codex's own per-hook ``trustStatus`` / ``statusMessage`` /
    ``isManaged`` (which the trust handshake otherwise discards) so the
    error explains *why* a hook could not be trusted — e.g. a managed
    requirement rejecting a user hook, or an old codex that omits
    ``trustStatus`` entirely.

    :param hooks: Untrusted Omnigent hook metadata dicts from
        ``hooks/list``.
    :returns: A semicolon-joined per-hook detail string.
    """
    return "; ".join(
        f"{h.get('key')!r} trustStatus={h.get('trustStatus')!r} "
        f"isManaged={h.get('isManaged')!r} statusMessage={h.get('statusMessage')!r}"
        for h in hooks
    )


async def _persist_hook_trust(request: CodexRequestFn, untrusted: Sequence[_JsonObject]) -> None:
    """
    Write ``hooks.state.<key>.trusted_hash`` for each untrusted hook.

    Persisted trust is the *only* mechanism that makes a hook run under
    ``codex app-server``: the ``--dangerously-bypass-hook-trust`` CLI flag
    is honored by the interactive/exec paths only, so app-server threads
    silently skip anything left ``untrusted``.

    :param request: Bound app-server JSON-RPC request coroutine, e.g.
        ``client.request``.
    :param untrusted: Hook metadata dicts from ``hooks/list`` carrying
        ``key`` and ``currentHash``.
    :returns: None.
    """
    trust_value = {
        str(h["key"]): {"trusted_hash": h["currentHash"]}
        for h in untrusted
        if h.get("key") and h.get("currentHash")
    }
    if not trust_value:
        return
    await request(
        "config/batchWrite",
        {
            "edits": [
                {
                    "keyPath": "hooks.state",
                    "mergeStrategy": "upsert",
                    "value": trust_value,
                }
            ],
            "reloadUserConfig": True,
        },
    )


async def trust_codex_router_hooks(request: CodexRequestFn, *, cwd: str) -> list[str]:
    """
    Trust the generated subagent-routing hooks so codex runs them.

    Codex skips untrusted hooks without a word, which for the routing gate
    is a fail-open, and app-server threads honor persisted trust only (the
    ``--dangerously-bypass-hook-trust`` flag covers the interactive /
    ``exec`` paths, not this one), so the handshake is the only way in.

    The routing gate (``PreToolUse`` on the spawn tool) lives in the same
    generated ``hooks.json`` as the policy hook but under a different
    module, so the policy trust pass leaves it ``untrusted``. Same
    ``hooks/list`` → ``config/batchWrite`` flow, but best-effort: a
    routing-trust failure must not disable policy enforcement, so it is
    reported instead of raised.

    :param request: Bound app-server JSON-RPC request coroutine, e.g.
        ``client.request`` (or the SDK executor's ``_request``).
    :param cwd: The session cwd the hooks are scoped to, e.g.
        ``"/home/user/repo"``.
    :returns: Keys of routing hooks still untrusted afterwards; empty when
        every routing hook is trusted (or none are registered).
    """
    listed = await request("hooks/list", {"cwds": [cwd]})
    ours = _our_hooks_from_list(listed, cwd, _CODEX_ROUTER_HOOK_MODULE)
    if not ours:
        _logger.info(
            "codex subagent-routing hooks: none discovered for cwd %s (%s)",
            cwd,
            _hooks_list_diagnostics(listed, cwd),
        )
        return []
    untrusted = [h for h in ours if h.get("trustStatus") not in _TRUSTED_HOOK_STATUSES]
    if not untrusted:
        _logger.info(
            "codex subagent-routing hooks: all %d already trusted for cwd %s", len(ours), cwd
        )
        return []
    await _persist_hook_trust(request, untrusted)
    relisted = await request("hooks/list", {"cwds": [cwd]})
    still_untrusted = [
        h
        for h in _our_hooks_from_list(relisted, cwd, _CODEX_ROUTER_HOOK_MODULE)
        if h.get("trustStatus") not in _TRUSTED_HOOK_STATUSES
    ]
    if still_untrusted:
        _logger.warning(
            "codex subagent-routing hooks still untrusted after config/batchWrite; "
            "native subagent routing will NOT be enforced: %s",
            _untrusted_hook_detail(still_untrusted),
        )
        return [str(h.get("key")) for h in still_untrusted]
    _logger.info(
        "codex subagent-routing hooks trusted (%d of %d newly): %s",
        len(untrusted),
        len(ours),
        ", ".join(sorted(str(h.get("eventName")) for h in ours)),
    )
    return []


async def trust_native_policy_hooks(client: CodexAppServerClient, *, cwd: str) -> None:
    """
    Trust the Omnigent policy hook so codex actually runs it.

    Runs the same flow codex's TUI uses for hook trust: ``hooks/list``
    to read each hook's content hash, then ``config/batchWrite`` (with
    ``reloadUserConfig`` so loaded threads hot-reload) writing
    ``hooks.state.<key>.trusted_hash = currentHash``. Re-lists and
    verifies every Omnigent hook ended ``trusted``/``managed``.

    :param client: A connected Codex app-server client.
    :param cwd: The session cwd the hooks are scoped to, e.g.
        ``"/home/user/repo"``.
    :returns: None.
    :raises RuntimeError: If no Omnigent hook is discovered (it was
        not registered or not loaded) or if a hook remains untrusted
        after the trust write. Either is a silent fail-open for a policy
        gate, so it must fail loud.
    """
    listed = await client.request("hooks/list", {"cwds": [cwd]})
    ours = _our_policy_hooks_from_list(listed, cwd)
    if not ours:
        raise RuntimeError(
            f"Omnigent policy hook was not discovered for cwd {cwd!r}; "
            "tool-call policy enforcement would silently not run. "
            f"{_hooks_list_diagnostics(listed, cwd)}."
        )
    untrusted = [h for h in ours if h.get("trustStatus") not in _TRUSTED_HOOK_STATUSES]
    if not untrusted:
        return
    await _persist_hook_trust(client.request, untrusted)
    relisted = await client.request("hooks/list", {"cwds": [cwd]})
    still_untrusted = [
        h
        for h in _our_policy_hooks_from_list(relisted, cwd)
        if h.get("trustStatus") not in _TRUSTED_HOOK_STATUSES
    ]
    if still_untrusted:
        missing_protocol = any(
            h.get("currentHash") is None or h.get("trustStatus") is None for h in still_untrusted
        )
        minimum = _format_codex_version(_MIN_POLICY_HOOK_CODEX_VERSION)
        hint = (
            " The hooks/list metadata is missing currentHash/trustStatus, so the "
            f"codex CLI is older than {minimum} (upgrade codex)."
            if missing_protocol
            else ""
        )
        raise RuntimeError(
            "Omnigent policy hook still untrusted after config/batchWrite; "
            "tool-call policy enforcement would not run. Untrusted hooks: "
            f"{_untrusted_hook_detail(still_untrusted)}.{hint}"
        )


def _trust_codex_project(codex_home: Path, cwd: Path) -> None:
    """
    Trust a runner-selected workspace in the private Codex config.

    Codex 0.146 introduced a project-trust screen before the remote TUI
    creates its thread. Headless sessions cannot answer it, so startup waits
    until Omnigent reports a timeout. The config is already a private copy;
    this never modifies the user's shared ``~/.codex/config.toml``.

    :param codex_home: Private per-session ``CODEX_HOME`` directory.
    :param cwd: Workspace selected for this runner-owned session.
    :returns: None.
    """
    config_path = codex_home / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    document = tomlkit.parse(existing) if existing else tomlkit.document()
    projects = document.get("projects")
    if projects is None:
        projects = tomlkit.table()
        document["projects"] = projects
    project_key = str(cwd.resolve())
    project = projects.get(project_key)
    if project is None:
        project = tomlkit.table()
        projects[project_key] = project
    project["trust_level"] = "trusted"
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


@dataclass(frozen=True)
class _DatabricksLaunchMaterialization:
    """
    The Databricks-profile pieces of a Codex launch, shared by the real
    app-server build and the model-options probe so the two cannot drift.

    :param config_overrides: ``-c`` overrides routing Codex through the
        profile's AI Gateway (provider block + auth command + model pin).
    :param model: The model the overrides pin, e.g. ``"databricks-gpt-5-4"``
        — the explicit *model* when given, else the catalog default.
    :param host: The profile's workspace origin for ``DATABRICKS_HOST``.
    """

    config_overrides: list[str]
    model: str
    host: str


def _databricks_launch_materialization(
    *, model: str | None, profile: str
) -> _DatabricksLaunchMaterialization:
    """
    Resolve the Databricks-profile routing pieces of a Codex launch.

    Uses the profile's own host so the gateway base URL matches the token
    the profile-pinned auth command mints; a ``DATABRICKS_HOST`` override in
    the runner env must not point the base URL at another workspace.

    :param model: Optional explicit model pin; ``None`` resolves the
        catalog default.
    :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
    :returns: The materialized overrides, pinned model, and host.
    :raises OSError: When the profile resolves no workspace host.
    """
    host = _databricks_gateway_host(profile)
    if not host:
        raise OSError(
            f"Native Codex with Databricks profile {profile!r} (from your "
            "provider config) requires a matching ~/.databrickscfg section "
            "with a host visible to the runner process."
        )
    host = host.rstrip("/")
    # Resolve against what the workspace actually serves (live UC listing →
    # ucode state → bundled catalog), never the bundled catalog alone — its
    # legacy ``databricks-`` spellings can 501 on today's gateway.
    resolved_model = _resolve_databricks_codex_model(host, profile, model)
    return _DatabricksLaunchMaterialization(
        config_overrides=_databricks_codex_config_overrides(
            model=resolved_model,
            base_url=_databricks_codex_base_url(host),
            auth_command=_databricks_codex_auth_command(host, profile),
        ),
        model=resolved_model,
        host=host,
    )


# DATABRICKS-PATCH(codex-live-model-discovery)
def _resolve_databricks_codex_model(host: str, profile: str, requested: str | None) -> str:
    """Resolve the codex launch model against what the workspace serves.

    Codex used to take its model from the bundled MLflow catalog — a
    third-party listing whose Databricks ids carry the legacy
    ``databricks-`` spelling the gateway now answers with ``501
    NOT_IMPLEMENTED ... Use Unity Catalog model services (v3)`` — so a launch
    could pin a model the workspace will not serve. Resolve from the workspace
    instead, as claude-native already does: the live Unity Catalog listing,
    then ucode's cached copy of it, then the bundled catalog as the documented
    last resort.

    An explicit model is matched against the servable ids, so a legacy
    ``model_override`` persisted before this change still launches; one the
    workspace does not serve passes through untouched, because the gateway's
    error beats a silent substitution.

    :param host: Workspace origin, e.g. ``"https://example.com"``.
    :param profile: Databricks CLI profile backing the launch.
    :param requested: Explicit model id, or ``None`` to take the newest
        servable one.
    :returns: The model id to pin on the codex launch.
    """
    from omnigent.databricks_model_discovery import (
        discover_databricks_codex_models,
        select_servable_model,
    )

    servable: tuple[str, ...] = ()
    try:
        from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

        creds = resolve_databricks_workspace(profile)
        # Discover against the host the launch actually posts to. This resolver
        # honors ``DATABRICKS_HOST`` while the launch host comes from the
        # profile section alone (``_databricks_gateway_host``), so using
        # ``creds.host`` here can pin a model discovered on workspace A onto a
        # launch targeting workspace B. A token that does not match ``host``
        # simply fails the listing and drops to the ucode-state fallback below,
        # which is already keyed by ``host``.
        servable = discover_databricks_codex_models(host, creds.token)
    except Exception:  # noqa: BLE001 — cached ucode state is the launch fallback
        _logger.warning(
            "native-codex: live Databricks model discovery failed for profile %r; "
            "falling back to ucode state",
            profile,
            exc_info=True,
        )
        try:
            from omnigent.onboarding.ucode_state import read_ucode_state

            workspace_state = read_ucode_state(host)
            if workspace_state is not None:
                servable = tuple(workspace_state.codex_models)
        except Exception:  # noqa: BLE001 — the bundled catalog is the last resort
            _logger.warning(
                "native-codex: could not read ucode state for %r", profile, exc_info=True
            )

    if requested:
        return select_servable_model(requested, servable) or requested
    if servable:
        return servable[0]
    return model_catalog.resolve_catalog_model("databricks", family="openai").model_id


def build_codex_native_server(
    *,
    socket_path: Path,
    codex_home: Path,
    cwd: Path,
    model: str | None,
    profile: str | None,
    bridge_dir: Path,
    ap_server_url: str | None = None,
    ap_auth_headers: dict[str, str] | None = None,
    python_executable: str | None = None,
    codex_path: str | None = None,
    extra_config_overrides: list[str] | None = None,
    developer_instructions: str | None = None,
    bypass_sandbox: bool = False,
    trust_project: bool = False,
) -> CodexNativeAppServer:
    """
    Build a configured native Codex app-server process wrapper.

    :param socket_path: Unix socket path for the app-server.
    :param codex_home: Private per-session ``CODEX_HOME`` path.
    :param cwd: Working directory for Codex, e.g. the user's repo.
    :param model: Optional Codex model id, e.g. ``"gpt-5.4-mini"``.
    :param profile: Optional Databricks CLI profile, e.g.
        ``"<your-profile>"``.
    :param bridge_dir: Native Codex bridge directory; the policy hook is
        pointed at it and reads the session id + Omnigent coordinates from it.
    :param ap_server_url: Omnigent server base URL the policy hook POSTs tool
        calls to, e.g. ``"http://127.0.0.1:8787"``. ``None`` registers
        the hook but writes no Omnigent coordinates (hook no-ops).
    :param ap_auth_headers: Outbound auth headers for the policy hook's
        Omnigent requests, e.g. ``{"Authorization": "Bearer <token>"}``.
    :param python_executable: Python executable the policy hook command
        runs. ``None`` uses :data:`sys.executable`.
    :param codex_path: Optional executable override. ``None`` searches
        ``PATH``.
    :param extra_config_overrides: Additional ``-c`` config overrides
        appended after Databricks routing overrides, e.g. MCP server
        registration for the Omnigent tool relay.
    :param developer_instructions: Optional framework-owned instructions
        appended to Codex's private per-session config.
    :param bypass_sandbox: When ``True``, append config overrides that put
        the app-server's threads into the full-bypass stance
        (``approval_policy="never"`` + ``sandbox_mode="danger-full-access"``)
        so the chat/forwarder seam matches the ``--remote`` TUI launched
        with ``--dangerously-bypass-approvals-and-sandbox``. DANGEROUS:
        disables both approval prompts and the command sandbox; gated
        behind an explicit, typed-confirmation opt-in in the web UI.
        Default ``False``. See issue #657.
    :param trust_project: Whether to trust ``cwd`` in the private session
        config before app-server startup. Intended for runner-owned headless
        sessions whose hidden TUI cannot answer Codex's project-trust prompt.
    :returns: Configured app-server process wrapper.
    :raises ImportError: If no Codex CLI is available.
    :raises OSError: If Databricks routing was requested but no
        credentials can be resolved.
    """
    resolved_codex = codex_path or _find_codex_cli()
    if not resolved_codex:
        raise ImportError(
            "Native Codex requires the 'codex' CLI on PATH. If codex is "
            "installed on a PATH the host daemon didn't inherit (e.g. an "
            "nvm-managed bin dir), set OMNIGENT_CODEX_PATH=/path/to/codex."
        )
    env = _clean_codex_env()
    config_overrides: list[str] = []
    pinned_model = model
    if profile is not None:
        databricks = _databricks_launch_materialization(model=model, profile=profile)
        config_overrides.extend(databricks.config_overrides)
        env["DATABRICKS_HOST"] = databricks.host
        # A launch that names no model still routes through the profile's
        # resolved model via ``-c model=``, which outranks the config.toml
        # copied from the user's shared home. Pin that model in codex's own
        # spelling — the vocabulary config.toml and its readers use — so the
        # forwarder mirror and cost gate report the model this session runs
        # instead of whatever the shared file was last left on.
        pinned_model = codex_spawn_model(databricks.model) or databricks.model
    if extra_config_overrides:
        config_overrides.extend(extra_config_overrides)
    if bypass_sandbox:
        # Mirror the --remote TUI's --dangerously-bypass-approvals-and-sandbox
        # on the app-server threads: never prompt for approval, and run
        # commands with no command sandbox. Emitted last so it wins over any
        # earlier approval/sandbox override.
        config_overrides.extend(
            [
                'approval_policy="never"',
                'sandbox_mode="danger-full-access"',
            ]
        )
    # Every launch is explicit: the resolved model rides argv (``-c model=``)
    # AND the private config copy's ``model =`` line (pinned in ``start``),
    # both written from this one value — so a stale line copied from the
    # user's shared config can never govern a session, and the two artifacts
    # cannot drift.
    if pinned_model and not any(
        override.split("=", 1)[0] == "model" for override in config_overrides
    ):
        config_overrides.append(f"model={json.dumps(pinned_model)}")
    return CodexNativeAppServer(
        codex_path=resolved_codex,
        socket_path=socket_path,
        codex_home=codex_home,
        env=env,
        config_overrides=config_overrides,
        cwd=cwd,
        bridge_dir=bridge_dir,
        developer_instructions=developer_instructions,
        ap_server_url=ap_server_url,
        ap_auth_headers=ap_auth_headers,
        python_executable=python_executable,
        pinned_model=pinned_model,
        trust_project=trust_project,
    )


@dataclass(frozen=True)
class NativeCodexLaunch:
    """How a native Codex terminal should be launched, across all offerings.

    Resolved by :func:`resolve_native_codex_launch` so a native Codex
    session honors ``configure harnesses`` like the in-process codex harness.

    :param config_overrides: Codex ``-c`` overrides that route through a
        generic provider (``model_provider`` + base_url + auth + wire);
        empty for the Databricks-profile and CLI-login paths.
    :param model: Model id to pin, or ``None`` to keep Codex's default.
    :param profile: Databricks profile for the ucode path, or ``None`` (a
        generic provider routes via *config_overrides*; CLI login uses
        neither).
    :param summary: Human-readable one-line description of the routing
        outcome (provider / profile / model, or the login-fallback state),
        set at resolution time and surfaced in the startup-timeout error so
        hosted users can diagnose without runner-log access (see #2745).
    """

    config_overrides: list[str]
    model: str | None
    profile: str | None
    summary: str = ""


def codex_session_meta_model_provider(launch: NativeCodexLaunch) -> str:
    """Return the provider id a launch routes through, for rollout synthesis.

    Synthesized rollouts (fork carry-history, cross-machine cold resume) must
    name the model provider in ``session_meta``: codex >= 0.133 backfills
    rollouts written before app-server start into its thread-store sqlite,
    and ``thread/resume`` of a backfilled row whose provider is empty or
    unresolvable fails config load (``Model provider `` not found``),
    silently dropping the carried history. The correct value is whatever
    provider the launch itself routes through:

    - a ``model_provider`` ``-c`` override (cli-config / key / gateway /
      local providers) pins it explicitly — the override value is a TOML
      basic string, which is also valid JSON;
    - a Databricks profile launch carries no override here; the provider
      table is generated at app-server start under the fixed
      ``omnigent_databricks`` id (see ``_databricks_codex_config_overrides``);
    - otherwise the launch defers to Codex's own login, the built-in
      ``openai`` provider.

    :param launch: Resolved native-Codex launch, e.g. one returned by
        :func:`resolve_native_codex_launch`.
    :returns: Provider id for ``session_meta.model_provider``, e.g.
        ``"omnigent_databricks"``.
    """
    prefix = "model_provider="
    for override in launch.config_overrides:
        if override.startswith(prefix):
            decoded: object = json.loads(override.removeprefix(prefix))
            if not isinstance(decoded, str):
                raise ValueError("model_provider override must decode to a string")
            return decoded
    if launch.profile is not None:
        return "omnigent_databricks"
    return "openai"


def native_codex_launch_base_url(launch: NativeCodexLaunch) -> str | None:
    """Inference base URL a resolved launch pins, or None when it defers to Codex's own login.

    Mirrors how the launch is actually applied: the Databricks-profile branch of
    :func:`build_native_codex_app` derives the base URL from the profile host,
    while a generic provider carries it inside the generated
    ``model_providers.…`` config override.

    :param launch: Resolved native-Codex launch, e.g. one returned by
        :func:`resolve_native_codex_launch`.
    :returns: The base URL the launch routes through, or ``None`` when the
        launch pins none.
    """
    if launch.profile is not None:
        host = _databricks_gateway_host(launch.profile)
        if not host:
            return None
        return _databricks_codex_base_url(host.rstrip("/"))
    for override in launch.config_overrides:
        _, sep, table = override.partition("=")
        if not sep or not override.startswith("model_providers."):
            continue
        marker = "base_url="
        index = table.find(marker)
        if index < 0:
            continue
        decoder = json.JSONDecoder()
        try:
            base_url, _ = decoder.raw_decode(table[index + len(marker) :])
        except ValueError:
            continue
        if isinstance(base_url, str):
            return base_url
    # A cli-config entry pins only a provider *name*; its table (with the
    # base_url) lives in the user's shared ~/.codex/config.toml. Read that
    # file to resolve the base URL a cli-config launch actually routes through.
    if _launch_pins_model_provider(launch):
        return _cli_config_provider_base_url(codex_session_meta_model_provider(launch))
    # No provider pinned at all: the deliberate config-default path leaves
    # overrides empty so Codex uses its own config.toml top-level
    # ``model_provider`` default (a Databricks-wide setup). Resolve that.
    return _config_default_provider_base_url()


def _launch_pins_model_provider(launch: NativeCodexLaunch) -> bool:
    """Whether a launch carries an explicit ``model_provider=`` override.

    Distinguishes a launch that pins a provider name (cli-config, or the
    literal ``model_provider="openai"`` the subscription / dismissed paths
    set) from the empty-override config-default launch, which pins none.
    """
    return any(override.startswith("model_provider=") for override in launch.config_overrides)


def _config_default_provider_base_url() -> str | None:
    """Base URL Codex's config.toml top-level ``model_provider`` default routes to.

    When omnigent pins no provider, Codex falls back to the top-level
    ``model_provider`` in the user's shared ``config.toml`` — unless the user
    dismissed that default, which pins Codex's built-in ``openai`` instead.

    :returns: The default provider table's ``base_url``, or ``None`` when the
        default is dismissed, absent, or unreadable.
    """
    import tomllib

    from omnigent.inner.codex_executor import _codex_home_config_source_from_env
    from omnigent.onboarding.detected import codex_config_provider_dismissed
    from omnigent.onboarding.provider_config import load_config

    if codex_config_provider_dismissed(load_config()):
        return None
    config_path = _codex_home_config_source_from_env() / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        provider_name = data["model_provider"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    if not isinstance(provider_name, str):
        return None
    return _config_toml_provider_base_url(provider_name)


def _cli_config_provider_base_url(provider_name: str) -> str | None:
    """Base URL a cli-config provider name resolves to in the user's codex config.

    A ``cli-config`` launch pins only a ``model_provider`` name; the provider
    table lives in the user's shared ``config.toml``, which the launch never
    inlines. Read it here so the gateway-inference probe can see the URL.

    Only genuine cli-config provider names are looked up: ``"openai"`` is
    Codex's own login (no pinned AIGW) and ``"omnigent_databricks"`` is the
    profile branch's generated id, so both return ``None``.

    :param provider_name: Provider id from
        :func:`codex_session_meta_model_provider`.
    :returns: The provider table's ``base_url``, or ``None`` when it cannot be
        read.
    """
    if provider_name in ("openai", "omnigent_databricks"):
        return None
    return _config_toml_provider_base_url(provider_name)


def _config_toml_provider_base_url(provider_name: str) -> str | None:
    """Read ``[model_providers.<provider_name>].base_url`` from the shared config.toml.

    :param provider_name: A provider table key in the user's ``config.toml``.
    :returns: That table's ``base_url``, or ``None`` when it cannot be read.
    """
    import tomllib

    from omnigent.inner.codex_executor import _codex_home_config_source_from_env

    config_path = _codex_home_config_source_from_env() / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        base_url = data["model_providers"][provider_name]["base_url"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    return base_url if isinstance(base_url, str) else None


def _codex_provider_launch(entry: ProviderEntry, model: str | None) -> NativeCodexLaunch | None:
    """Build a native-Codex launch that routes through a single provider entry.

    Mirrors the in-process codex harness routing for the ``openai`` surface:

    - a ``databricks`` entry routes via its ucode profile (the Databricks
      branch of :func:`build_native_codex_app` turns the profile into config
      overrides), so the launch carries ``profile`` and empty overrides;
    - a ``cli-config`` entry routes via a single ``model_provider`` ``-c``
      override pinning the custom provider its ``~/.codex/config.toml``
      defines (the provider table + credential live in that file, which the
      native server bridges into the session ``CODEX_HOME``);
    - a ``key`` / ``gateway`` / ``local`` entry routes via a generated
      ``model_provider`` ``-c`` override (base_url + bearer-token auth command
      + wire protocol).

    Returns ``None`` when *entry* cannot route Codex on its own — a
    ``subscription`` entry (which defers to Codex's own stored login), a
    provider that does not serve the ``openai`` surface, or a key/gateway/local
    entry with no credential or whose secret reference does not resolve in this
    process. A ``None`` lets the caller fall through to another provider rather
    than crash at terminal launch or strand the user at Codex's login screen.

    :param entry: The provider entry to route through, e.g. a ``key`` entry for
        ``openai`` or a ``databricks`` entry.
    :param model: An explicit/session model override that wins over the
        provider's default model, e.g. ``"gpt-5.5"``; ``None`` keeps the
        provider's default.
    :returns: A routable :class:`NativeCodexLaunch`, or ``None`` when *entry*
        cannot route without Codex's own login.
    """
    from omnigent.errors import OmnigentError
    from omnigent.onboarding.provider_config import (
        CLI_CONFIG_KIND,
        DATABRICKS_KIND,
        GATEWAY_KIND,
        KEY_KIND,
        LOCAL_KIND,
        OPENAI_FAMILY,
    )

    if entry.kind == DATABRICKS_KIND:
        return NativeCodexLaunch(
            config_overrides=[],
            model=model,
            profile=entry.profile,
            summary=f"Databricks ucode profile {entry.profile!r}",
        )
    if entry.kind == CLI_CONFIG_KIND:
        # Pin the config.toml-defined provider by name; its table (and
        # credential) ride along via the bridged config.toml. json.dumps
        # yields a valid TOML basic string for the -c override value.
        return NativeCodexLaunch(
            config_overrides=[f"model_provider={json.dumps(entry.model_provider)}"],
            model=model,
            profile=None,
            summary=(
                f"provider {entry.name!r} via cli-config (model_provider={entry.model_provider!r})"
            ),
        )
    if entry.kind not in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
        return None
    try:
        family = entry.family(OPENAI_FAMILY)
    except OmnigentError:
        # The credential reference (``env:...`` / ``keychain:...``) does not
        # resolve in this process — treat the provider as unroutable so the
        # caller can try another instead of crashing at terminal launch.
        return None
    if family is None:
        return None
    if family.auth_command:
        auth_command = family.auth_command
    elif family.api_key:
        auth_command = f"printf %s {shlex.quote(family.api_key)}"
    else:
        # Serves openai but carries no usable credential.
        return None
    pinned = model or family.default_model
    overrides = _provider_codex_config_overrides(
        model=pinned,
        base_url=family.base_url,
        auth_command=auth_command,
        wire_api=family.wire_api or "responses",
    )
    return NativeCodexLaunch(
        config_overrides=overrides,
        model=pinned,
        profile=None,
        summary=f"provider {entry.name!r} (model={pinned})",
    )


def _first_routable_codex_provider(
    config: dict[str, object], *, exclude: str, model: str | None
) -> NativeCodexLaunch | None:
    """Find a provider other than *exclude* that can route a native Codex launch.

    Used when the resolved default is a ``subscription`` entry but Codex has no
    usable stored login: rather than strand the user at Codex's login screen,
    route through the first *other* provider serving the ``openai`` surface that
    can produce a launch (a real key/gateway/local credential or a Databricks
    profile). Explicit providers are tried before ambient detections — the
    user's config is authoritative — and ambient detections (e.g. a real
    ``OPENAI_API_KEY`` in the environment) are honored only as a fallback.

    This does **not** mutate the persisted default: the dead subscription
    remains the configured default, so if the user re-logs into Codex it
    resumes being used. This is a runtime safety net, not a default change.

    :param config: The explicit parsed config mapping (``providers:`` block);
        ambient detections are merged in read-only.
    :param exclude: The provider name to skip — the dead subscription default,
        e.g. ``"codex"``.
    :param model: An explicit/session model override, or ``None``.
    :returns: The first routable :class:`NativeCodexLaunch`, or ``None`` when no
        other provider can route Codex.
    """
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        OPENAI_FAMILY,
        load_providers,
        provider_families,
    )

    explicit_providers = load_providers(config)
    merged_providers = load_providers(effective_config_with_detected(config))
    # Explicit first (authoritative), then ambient-only detections.
    ordered = list(explicit_providers.items()) + [
        (name, entry) for name, entry in merged_providers.items() if name not in explicit_providers
    ]
    for name, candidate in ordered:
        if name == exclude:
            continue
        if OPENAI_FAMILY not in provider_families(candidate):
            continue
        launch = _codex_provider_launch(candidate, model)
        if launch is not None:
            _logger.warning(
                "native-codex: subscription default %r has no usable Codex login; "
                "routing through provider %r instead.",
                exclude,
                name,
            )
            return launch
    return None


def _resolve_subscription_launch(
    entry: ProviderEntry, model: str | None, explicit: dict[str, object]
) -> NativeCodexLaunch:
    """Resolve a native-Codex launch when the Codex default is a ``subscription``.

    A subscription defers to Codex's own stored login — correct only when Codex
    is actually logged in. An empty / logged-out ``auth.json`` would otherwise
    route to Codex's login screen even though the user configured a real
    credential, so when Codex is not logged in this falls through to the first
    other configured provider that can route (runtime only — the persisted
    default is untouched). With no usable login and nothing to fall through to,
    Codex's own login is the correct outcome (the user must re-authenticate).

    :param entry: The resolved ``subscription`` provider entry (the Codex
        default), e.g. a ``codex`` CLI-login entry.
    :param model: An explicit/session model override, or ``None``.
    :param explicit: The explicit parsed config mapping (``providers:`` block),
        used for the fall-through search over other configured/detected
        providers.
    :returns: The resolved :class:`NativeCodexLaunch`.
    """
    from omnigent.onboarding.ambient import codex_auth_has_credential

    # Pin codex's built-in ``openai`` provider: the bridged config.toml may
    # set a custom default ``model_provider`` (e.g. isaac's Databricks AI
    # Gateway), which would silently hijack a Subscription selection. A
    # no-op when the user's config sets no custom default.
    subscription_overrides = ['model_provider="openai"']
    # Resolve against the same CODEX_HOME the native server bridges from
    # (``_populate_codex_home_config``) so this "is Codex logged in?" check reads
    # the exact auth.json the launched Codex process will use.
    real_codex_home = _codex_home_config_source_from_env()
    if codex_auth_has_credential(real_codex_home / "auth.json"):
        _logger.info(
            "native-codex routing: Codex CLI login (subscription provider %r; Codex is logged in)",
            entry.name,
        )
        return NativeCodexLaunch(
            config_overrides=subscription_overrides,
            model=model,
            profile=None,
            summary=f"Codex CLI login (subscription provider {entry.name!r}; Codex is logged in)",
        )
    fallback = _first_routable_codex_provider(explicit, exclude=entry.name, model=model)
    if fallback is not None:
        return fallback
    _logger.info(
        "native-codex routing: Codex CLI login (subscription provider %r has no usable "
        "Codex login and no alternative provider is configured)",
        entry.name,
    )
    return NativeCodexLaunch(
        config_overrides=subscription_overrides,
        model=model,
        profile=None,
        summary=(
            f"Codex CLI login (subscription provider {entry.name!r} has no usable Codex "
            "login and no alternative provider is configured) — the TUI likely renders "
            "the sign-in screen and never starts a thread"
        ),
    )


def resolve_native_codex_launch(
    *, model: str | None, spec: AgentSpec | None = None
) -> NativeCodexLaunch:
    """Resolve the native Codex launch config across all offerings.

    Mirrors the in-process codex harness routing precedence
    (:func:`omnigent.runtime.workflow._resolve_provider_for_build`) for the
    ``openai`` surface, so ``omnigent codex`` and a host-spawned native
    Codex session route through ``omnigent setup``:

    0. (with *spec*) a spec-level credential — ``executor.auth`` naming a
       provider (:class:`~omnigent.spec.types.ProviderAuth`, fails loud when
       undeclared), a spec :class:`~omnigent.spec.types.DatabricksAuth`, or a
       legacy ``executor.profile`` / ``executor.config.profile`` — resolved
       through :func:`~omnigent.runtime.workflow._resolve_provider_for_build`
       itself, the same resolver the in-process harness uses, so a spec that
       routes in-process routes natively too (a spec ``ApiKeyAuth`` resolves
       to ``None`` for every harness — the resolver leaves bare keys to the
       claude-sdk / openai-agents builders — so codex-native falls through
       exactly as in-process codex does);

    1. an explicit per-family default provider →
       - ``key`` / ``gateway`` / ``local`` → provider ``-c`` overrides
         (base_url + token + wire), ``profile=None``;
       - ``databricks`` → the ucode profile path (its profile);
       - ``subscription`` → the Codex CLI's own stored login (no overrides)
         **when Codex is actually logged in**; otherwise (empty / logged-out
         ``auth.json``) fall through to the first other configured provider
         that can route, so a real credential is not shadowed by a dead
         subscription default;
    2. else a global Databricks ``auth:`` block → ucode;
    3. else an ambient-detected provider (first run without configure);
    4. else the codex CLI's own login.

    Without a *spec* (or when the spec carries no spec-level credential),
    credentials are controlled by ``omnigent setup`` provider config (or the
    legacy global ``auth:`` block) exactly as before — there is no CLI/env
    profile override, and machine-level flows are unchanged.

    :param model: An explicit/session model override that wins over the
        provider's default model, or ``None``.
    :param spec: The custom agent spec launching this session, when there is
        one, so its ``executor.auth`` / legacy profile win over machine-level
        config (issue #2744 — parity with the in-process codex harness).
    :returns: The resolved :class:`NativeCodexLaunch`.
    """
    from omnigent.onboarding.ambient import codex_config_detection
    from omnigent.onboarding.detected import (
        dismissed_detection_names,
        effective_config_with_detected,
    )
    from omnigent.onboarding.provider_config import (
        SUBSCRIPTION_KIND,
        default_provider_for_harness,
        load_config,
    )
    from omnigent.runtime.workflow import _load_global_auth, _resolve_provider_for_build
    from omnigent.spec.types import DatabricksAuth

    explicit = load_config()
    config_detection = codex_config_detection()
    config_provider_dismissed = (
        config_detection is not None
        and config_detection.name in dismissed_detection_names(explicit)
    )
    # When the launch ends up on codex's own login with NO provider routing,
    # the bridged config.toml's custom default model_provider would still
    # apply — including one the user explicitly Removed (dismissed). Pin
    # codex's built-in provider in that case so the dismissal holds at run
    # time. An undetectable/undismissed custom provider keeps its routing.
    no_provider_overrides = ['model_provider="openai"'] if config_provider_dismissed else []
    if spec is not None and (
        spec.executor.auth is not None
        or spec.executor.profile
        or spec.executor.config.get("profile")
    ):
        # Spec-level credential (issue #2744): resolve it through the same
        # resolver the in-process codex harness uses, so switching a working
        # spec from ``harness: codex`` to ``codex-native`` keeps its auth
        # working. A named provider that is undeclared raises loud here
        # instead of parking the TUI on the sign-in screen for a 30s timeout.
        # A spec ``ApiKeyAuth`` resolves to ``None`` for every harness (the
        # shared resolver leaves bare keys to the claude-sdk / openai-agents
        # builders; the in-process codex builder has no ApiKeyAuth branch
        # either), so codex-native falls through to the machine-level chain
        # below exactly as in-process codex does — as does a spec credential
        # that cannot route openai.
        spec_entry = _resolve_provider_for_build(spec, harness_type="codex", for_launch=True)
        if spec_entry is not None:
            if spec_entry.kind == SUBSCRIPTION_KIND:
                # A spec-named subscription defers to Codex's own login,
                # logged in or not. The machine-default path would substitute
                # the first OTHER routable provider on a logged-out Codex
                # (:func:`_resolve_subscription_launch`) — never do that for
                # an explicit spec declaration: silently running a credential
                # the spec did not name is worse than the login screen.
                from omnigent.onboarding.ambient import codex_auth_has_credential

                if codex_auth_has_credential(_codex_home_config_source_from_env() / "auth.json"):
                    state = "Codex is logged in"
                else:
                    state = (
                        "Codex is not logged in — the TUI likely renders the "
                        "sign-in screen and never starts a thread"
                    )
                return NativeCodexLaunch(
                    config_overrides=['model_provider="openai"'],
                    model=model,
                    profile=None,
                    summary=f"Codex CLI login (spec provider {spec_entry.name!r}; {state})",
                )
            launch = _codex_provider_launch(spec_entry, model)
            if launch is not None:
                if launch.profile is not None:
                    _logger.info(
                        "native-codex routing: Databricks ucode profile %r (spec auth)",
                        launch.profile,
                    )
                else:
                    _logger.info(
                        "native-codex routing: provider %r (spec auth, model=%s)",
                        spec_entry.name,
                        launch.model,
                    )
                return launch
            _logger.warning(
                "native-codex: spec-level provider %r has no usable openai credential — "
                "falling back to machine-level resolution.",
                spec_entry.name,
            )
    entry = default_provider_for_harness(explicit, "codex")
    if entry is None:
        # No explicit provider default: global auth wins over ambient
        # (parity with _resolve_provider_for_build).
        global_auth = _load_global_auth()
        if isinstance(global_auth, DatabricksAuth):
            return NativeCodexLaunch(
                config_overrides=[],
                model=model,
                profile=global_auth.profile,
                summary=f"Databricks ucode profile {global_auth.profile!r} (global auth block)",
            )
        if global_auth is not None:
            return NativeCodexLaunch(
                config_overrides=[],
                model=model,
                profile=None,
                summary="Codex CLI login (global auth block, non-Databricks; no provider routing)",
            )
        entry = default_provider_for_harness(effective_config_with_detected(explicit), "codex")

    if (
        entry is None
        and config_detection is not None
        and config_detection.model_provider is not None
        and not config_provider_dismissed
    ):
        # An adopted cli-config entry can explicitly shadow the same ambient
        # detection without being marked the Omnigent default. Codex still
        # selects that provider from config.toml, so pin the already-resolved
        # detection instead of describing this as an OpenAI-login launch.
        # This keeps rollout metadata, app-server, and remote TUI routing on
        # one immutable provider selection during cold resume.
        provider_id = config_detection.model_provider
        _logger.info(
            "native-codex routing: config.toml provider %r (ambient fallback, model=%s)",
            provider_id,
            model,
        )
        return NativeCodexLaunch(
            config_overrides=[f"model_provider={json.dumps(provider_id)}"],
            model=model,
            profile=None,
            summary=f"Codex config.toml provider {provider_id!r} (ambient fallback)",
        )

    if entry is None:
        _logger.info(
            "native-codex routing: Codex CLI login (no provider configured for the Codex "
            "harness, no Databricks profile). Run `omnigent setup --no-internal-beta` to route "
            "through a provider."
        )
        return NativeCodexLaunch(
            config_overrides=no_provider_overrides,
            model=model,
            profile=None,
            summary=(
                "Codex CLI login (no provider configured for the codex harness, no "
                "Databricks profile) — the TUI likely renders the ChatGPT sign-in "
                "screen and never starts a thread; run `omnigent setup` to route through "
                "a provider"
            ),
        )
    if entry.kind == SUBSCRIPTION_KIND:
        return _resolve_subscription_launch(entry, model, explicit)

    launch = _codex_provider_launch(entry, model)
    if launch is not None:
        if launch.profile is not None:
            _logger.info("native-codex routing: Databricks ucode profile %r", launch.profile)
        else:
            _logger.info("native-codex routing: provider %r (model=%s)", entry.name, launch.model)
        return launch
    # Default provider can't route on its own (no openai surface / no usable
    # credential / unresolvable secret) → Codex's own login.
    _logger.warning(
        "native-codex: provider %r is the Codex default but has no usable openai "
        "credential — falling back to Codex's own login.",
        entry.name,
    )
    return NativeCodexLaunch(
        config_overrides=no_provider_overrides,
        model=model,
        profile=None,
        summary=(
            f"Codex CLI login (provider {entry.name!r} is the codex default but has no "
            "usable openai credential) — the TUI likely renders the sign-in screen "
            "and never starts a thread"
        ),
    )


def client_for_transport(
    transport: str,
    *,
    client_name: str = "omnigent",
) -> CodexAppServerClient:
    """
    Build an app-server client for a persisted transport string.

    The native Codex app-server is reachable over either a loopback
    WebSocket (``"ws://IP:PORT"`` / ``"wss://..."``) or a Unix socket
    path (legacy ``"/path/app-server.sock"``). Both the host-spawned
    runner and the local CLI now listen on ``ws://`` — the only
    transport Codex CLI ``app-server`` accepts since it dropped
    ``unix://`` support — and persist whichever was used as the bridge
    state's ``socket_path``. Every connect site (executor steering /
    interrupt / run_turn, forwarder, initial turn) routes through this
    one rule so a ``ws://`` transport is never mistakenly wrapped in
    ``Path(...)`` and dialed as a (nonexistent) Unix socket.

    :param transport: App-server transport from bridge state, e.g.
        ``"ws://127.0.0.1:9876"`` or
        ``"/home/u/.omnigent/codex-native/x/app-server.sock"``.
    :param client_name: App-server initialize-handshake client name,
        e.g. ``"omnigent-codex-native"``.
    :returns: A client configured for the transport (not yet connected).
    """
    if transport.startswith(("ws://", "wss://")):
        return CodexAppServerClient(ws_url=transport, client_name=client_name)
    return CodexAppServerClient(Path(transport), client_name=client_name)


def normalize_codex_permission_launch_args(
    terminal_launch_args: Sequence[str] | None,
) -> list[str]:
    """Complete legacy Full Access args with their approval policy."""
    args = list(terminal_launch_args or ())
    full_access = False
    has_approval_policy = "--dangerously-bypass-approvals-and-sandbox" in args
    index = 0
    while index < len(args):
        arg = args[index]
        assignment: str | None = None
        if arg in {"--ask-for-approval", "-a"} or arg.startswith(("--ask-for-approval=", "-a=")):
            has_approval_policy = True
        elif arg in {"--config", "-c"} and index + 1 < len(args):
            index += 1
            assignment = args[index]
        elif arg.startswith(("--config=", "-c=")):
            assignment = arg.split("=", 1)[1]
        if assignment is not None:
            key, _, raw_value = assignment.partition("=")
            if key.strip() == "approval_policy":
                has_approval_policy = True
            elif key.strip() == "default_permissions":
                full_access = _codex_config_string(raw_value) == ":danger-full-access"
        index += 1
    if full_access and not has_approval_policy:
        args.extend(["-c", 'approval_policy="never"'])
    return args


def _codex_config_string(raw_value: str) -> str:
    try:
        value = tomlkit.parse(f"value = {raw_value}")["value"]
    except Exception:  # noqa: BLE001 - Codex accepts some unquoted CLI config values.
        value = raw_value.strip().strip('"').strip("'")
    return value if isinstance(value, str) else ""


def _codex_resume_permission_params(terminal_launch_args: Sequence[str] | None) -> CodexParams:
    """Convert persisted Codex permission args into thread-resume overrides."""
    params: CodexParams = {}
    args = normalize_codex_permission_launch_args(terminal_launch_args)
    index = 0
    while index < len(args):
        arg = args[index]
        value: str | None = None
        if arg == "--dangerously-bypass-approvals-and-sandbox":
            params.update(approvalPolicy="never", sandbox="danger-full-access")
        elif arg in {"--ask-for-approval", "-a", "--sandbox", "-s"}:
            if index + 1 < len(args):
                value = args[index + 1]
                index += 1
            if value is not None:
                field = "approvalPolicy" if arg in {"--ask-for-approval", "-a"} else "sandbox"
                params[field] = value
        elif arg.startswith(("--ask-for-approval=", "-a=")):
            params["approvalPolicy"] = arg.split("=", 1)[1]
        elif arg.startswith(("--sandbox=", "-s=")):
            params["sandbox"] = arg.split("=", 1)[1]
        elif arg in {"--config", "-c"} and index + 1 < len(args):
            index += 1
            key, _, value = args[index].partition("=")
            _set_codex_resume_config_param(params, key, value)
        elif arg.startswith(("--config=", "-c=")):
            key, _, value = arg.split("=", 1)[1].partition("=")
            _set_codex_resume_config_param(params, key, value)
        index += 1
    if "permissions" in params:
        params.pop("sandbox", None)
    return params


def _set_codex_resume_config_param(params: CodexParams, key: str, raw_value: str) -> None:
    field = {
        "approval_policy": "approvalPolicy",
        "approvals_reviewer": "approvalsReviewer",
        "default_permissions": "permissions",
        "sandbox_mode": "sandbox",
    }.get(key.strip())
    if field is None:
        return
    value = _codex_config_string(raw_value)
    if value:
        params[field] = value


async def preload_codex_thread_for_resume(
    transport: str,
    thread_id: str,
    *,
    terminal_launch_args: Sequence[str] | None = None,
) -> None:
    """
    Load an existing Codex thread into a freshly started app-server.

    A rollout JSONL on disk is not enough for immediate web-message
    injection: a new app-server can reject ``turn/start`` with
    ``thread not found`` until some client has resumed the thread. This
    helper runs the lightweight ``thread/resume`` call before bridge
    state is exposed to the web-message executor.

    :param transport: App-server transport, e.g. ``"ws://127.0.0.1:9876"``
        or ``"/tmp/app-server.sock"``.
    :param thread_id: Codex thread id to load, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param terminal_launch_args: Persisted permission overrides for the resumed thread.
    :returns: None.
    :raises RuntimeError: If the app-server rejects the resume.
    """
    client = client_for_transport(
        transport,
        client_name="omnigent-codex-native-preload",
    )
    await client.connect()
    try:
        await client.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "excludeTurns": True,
                **_codex_resume_permission_params(terminal_launch_args),
            },
        )
    finally:
        await client.close()


def codex_terminal_env(app_server: CodexNativeAppServer) -> dict[str, str]:
    """
    Build terminal env overrides for the native Codex TUI.

    :param app_server: Running app-server wrapper.
    :returns: Environment variables for the terminal process.
    """
    return {
        key: value
        for key, value in {**app_server.env, "CODEX_HOME": str(app_server.codex_home)}.items()
        if key in {"CODEX_HOME", "DATABRICKS_HOST", "DATABRICKS_CODEX_TOKEN"}
        or key.startswith(("OPENAI_", "HTTP_", "HTTPS_", "NO_PROXY", "ALL_PROXY"))
    }


# Codex's full-bypass flag. Disables BOTH the approval prompts and the
# command sandbox in one switch. Verified against codex-cli 0.140.0-alpha.2:
# it is mutually exclusive with the approval flag only — passing
# ``--ask-for-approval`` (or its ``-a`` alias, in any spelling) alongside it
# aborts at startup with "cannot be used with
# --dangerously-bypass-approvals-and-sandbox". ``--sandbox`` / ``-s`` do NOT
# conflict (the bypass already implies ``danger-full-access``), so leaving
# them in is harmless. We strip BOTH anyway when bypass is on — the approval
# flag because it MUST go, the sandbox flag for hygiene so the launched arg
# list reflects a single coherent stance.
_CODEX_BYPASS_SANDBOX_FLAG = "--dangerously-bypass-approvals-and-sandbox"
_CODEX_BYPASS_HOOK_TRUST_FLAG = "--dangerously-bypass-hook-trust"
# Granular approval/sandbox flags to drop when bypass is on. The "Full
# access" / "Read only" approval presets emit the long ``--flag value`` form
# (see web CODEX_NATIVE_APPROVAL_MODES), but ``terminal_launch_args`` is
# client-supplied (validated only for count/length), so the short aliases
# (``-a`` / ``-s``) are included too: ``-a`` triggers the same startup abort
# as ``--ask-for-approval`` and must never reach codex. Each is matched in
# both the space-separated (``-a never``) and joined (``-a=never``) spellings
# by :func:`_strip_approval_sandbox_flags`.
_CODEX_APPROVAL_SANDBOX_FLAGS = frozenset({"--sandbox", "-s", "--ask-for-approval", "-a"})


def _strip_approval_sandbox_flags(codex_args: tuple[str, ...]) -> list[str]:
    """
    Drop granular approval/sandbox flags (and values) when bypass is on.

    Removes every flag in :data:`_CODEX_APPROVAL_SANDBOX_FLAGS` —
    ``--ask-for-approval`` / ``-a`` (which codex *rejects* alongside the
    bypass flag) and ``--sandbox`` / ``-s`` (harmless, dropped for hygiene).
    Both CLI spellings of each are handled:

    - ``--sandbox=read-only`` (single ``--flag=value`` token) is dropped
      whole.
    - ``--sandbox read-only`` (separate flag + value) drops the flag and
      its following value — but ONLY when that next token is actually a
      value (it does not itself start with ``-``). A following
      ``--something`` is a separate flag, not this flag's value, so it is
      left in place (e.g. ``("--sandbox", "--model", "gpt")`` keeps
      ``"--model", "gpt"``). A trailing flag at end-of-list is dropped
      cleanly with no value to consume.

    Any already-present bypass flag is also dropped so the caller can
    re-add a single canonical copy. Unrelated args (model, config
    overrides, ...) pass through untouched.

    :param codex_args: Raw Codex CLI args, e.g.
        ``("--sandbox", "read-only", "--model", "gpt-5.4-mini")``.
    :returns: ``codex_args`` with the conflicting flags removed, e.g.
        ``["--model", "gpt-5.4-mini"]``.
    """
    cleaned: list[str] = []
    i = 0
    n = len(codex_args)
    while i < n:
        arg = codex_args[i]
        if arg in _CODEX_APPROVAL_SANDBOX_FLAGS:
            # ``--flag value``: drop the flag, and consume the NEXT token as
            # its value ONLY when that token is a real value — it exists and
            # does not itself start with ``-`` (a leading ``-`` marks a
            # separate flag, e.g. ``("--sandbox", "--model", "gpt")`` keeps
            # ``--model``; a trailing flag at end-of-list consumes nothing).
            if i + 1 < n and not codex_args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
            continue
        if any(arg.startswith(f"{flag}=") for flag in _CODEX_APPROVAL_SANDBOX_FLAGS):
            # ``--flag=value`` single token: drop it whole, consume nothing.
            i += 1
            continue
        if arg == _CODEX_BYPASS_SANDBOX_FLAG:
            # Drop any pre-existing bypass flag; a single canonical copy is
            # re-added by the caller so it is never duplicated.
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    return cleaned


def build_codex_remote_args(
    *,
    codex_args: tuple[str, ...],
    thread_id: str | None,
    remote_url: str,
    config_overrides: tuple[str, ...] = (),
    bypass_sandbox: bool = False,
    bypass_hook_trust: bool = False,
) -> list[str]:
    """
    Build Codex CLI args for an app-server-backed TUI session.

    The TUI attaches to an already-running Codex app-server over its
    ``--remote`` transport so the terminal, the chat forwarder, and the
    web-UI message bridge all drive the same thread. The transport is
    passed verbatim so callers can attach over either a Unix socket
    (``"unix://PATH"``, the local CLI path) or a loopback TCP websocket
    (``"ws://IP:PORT"``, the host-spawned runner path — see
    :class:`CodexNativeAppServer` ``listen_url``).

    The ``config_overrides`` are the same ``-c key=value`` provider/model
    overrides the app-server is launched with. The ``--remote`` TUI is a
    *separate* process that loads its own config from ``CODEX_HOME`` and
    does NOT inherit the app-server's ``-c`` flags; without them it falls
    back to the built-in OpenAI provider, whose ``requires_openai_auth``
    is ``true``, so the TUI renders the first-run "Sign in with ChatGPT"
    onboarding screen and never creates a thread. On a host-spawned
    (web-UI-driven) session there is nobody at the terminal to dismiss
    that screen, so ``wait_for_thread_started`` times out and the session
    hangs in ``running`` with no response. Passing the provider overrides
    through makes the TUI resolve the Omnigent provider
    (``requires_openai_auth = false``), skip onboarding, and start the
    thread immediately. Codex global ``-c`` flags must precede the
    ``resume`` subcommand, so they are emitted first.

    :param codex_args: Raw Codex CLI args that precede the attach flags,
        e.g. ``("--model", "gpt-5.4-mini")``. Empty when the thread's own
        settings already cover everything.
    :param thread_id: Codex thread id to resume, e.g. ``"thread_abc123"``.
        ``None`` starts a fresh remote Codex TUI thread instead of
        resuming an existing one.
    :param remote_url: App-server endpoint the TUI attaches to, e.g.
        ``"unix:///home/user/.omnigent/codex-native/x/app-server.sock"``
        or ``"ws://127.0.0.1:9876"``.
    :param config_overrides: Codex ``-c`` config override values to apply
        to the TUI, e.g.
        ``('model="databricks-gpt-5-5"', 'model_provider="omnigent_databricks"')``.
        Each is emitted as a ``-c <value>`` global flag. Empty for a
        plain Codex-login launch that needs no provider routing.
    :param bypass_sandbox: When ``True``, emit a single
        ``--dangerously-bypass-approvals-and-sandbox`` flag and strip any
        conflicting ``--sandbox`` / ``--ask-for-approval`` pairs from
        *codex_args* (codex aborts at startup if the bypass flag is
        combined with either). DANGEROUS: this disables both the approval
        prompts and the command sandbox; it is gated behind an explicit,
        typed-confirmation opt-in in the web UI. Default ``False`` keeps
        the granular flags untouched. See issue #657.
    :param bypass_hook_trust: When ``True``, emit
        ``--dangerously-bypass-hook-trust`` so the TUI runs all enabled
        hooks without the interactive "Hooks need review" trust prompt.
        Intended for runner-owned headless sessions where the private
        ``CODEX_HOME`` is provisioned by Omnigent and there is no terminal
        user to answer the prompt. Default ``False`` for interactive
        ``omnigent codex`` sessions where the user faces the terminal and
        can accept hooks normally.
    :returns: Codex argv tail after the executable.
    """
    override_args: list[str] = []
    for override in config_overrides:
        if override.lstrip().startswith("model_providers."):
            raise ValueError(
                "Codex remote provider definitions must be materialized in CODEX_HOME"
            )
        override_args.extend(["-c", override])
    if bypass_sandbox:
        # Strip the conflicting granular flags, then prepend one canonical
        # bypass flag (a global flag, so it precedes any ``resume``).
        passthrough = [_CODEX_BYPASS_SANDBOX_FLAG, *_strip_approval_sandbox_flags(codex_args)]
    else:
        passthrough = normalize_codex_permission_launch_args(codex_args)
    if bypass_hook_trust:
        passthrough = [_CODEX_BYPASS_HOOK_TRUST_FLAG, *passthrough]
    if thread_id is None:
        return [*override_args, *passthrough, "--remote", remote_url]
    return [*override_args, *passthrough, "resume", "--remote", remote_url, thread_id]


def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """
    Send SIGTERM to a subprocess process group when possible.

    :param process: Subprocess handle to terminate.
    :returns: None.
    """
    _proc.terminate_tree(process)


def _process_group_id(process: asyncio.subprocess.Process) -> int:
    """
    Return the child process group id used for crash-safe reaping.

    :param process: Subprocess handle.
    :returns: Process group id, falling back to pid on non-POSIX hosts.
    """
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            return os.getpgid(process.pid)
    return process.pid


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """
    Send SIGKILL to a subprocess process group when possible.

    :param process: Subprocess handle to kill.
    :returns: None.
    """
    _proc.kill_tree(process)
