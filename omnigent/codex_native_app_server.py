"""Codex app-server process and JSON-RPC client for native TUI sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import websockets

if TYPE_CHECKING:
    from omnigent.onboarding.provider_config import ProviderEntry

from omnigent.codex_native_bridge import write_policy_hook_config
from omnigent.inner.codex_executor import (
    _CODEX_DEFAULT_REASONING_EFFORT,
    _OPENAI_CODEX_DEFAULT_MODEL,
    _clean_codex_env,
    _codex_cli_version,
    _codex_home_config_source_from_env,
    _databricks_codex_auth_command,
    _databricks_codex_base_url,
    _databricks_codex_config_overrides,
    _find_codex_cli,
    _populate_codex_home_config,
    _provider_codex_config_overrides,
)
from omnigent.inner.databricks_executor import _read_databrickscfg, _read_databrickscfg_host

_logger = logging.getLogger(__name__)

CodexMessage = dict[str, Any]
CodexParams = dict[str, Any]

_CONNECT_RETRY_DELAY_SECONDS = 0.05
_CONNECT_TIMEOUT_SECONDS = 10.0
_STDERR_CHUNK_LIMIT = 65536
_DATABRICKS_CODEX_DEFAULT_MODEL = "databricks-gpt-5-5"
_CODEX_DEFAULT_REASONING_CONFIG = (
    f"model_reasoning_effort={json.dumps(_CODEX_DEFAULT_REASONING_EFFORT)}"
)
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


def _codex_mcp_server_config_section(
    bridge_dir: Path,
    python_executable: str | None = None,
) -> str:
    """
    Build the generated Codex MCP server TOML section.

    :param bridge_dir: Bridge directory containing ``bridge.json`` and
        ``tool_relay.json``.
    :param python_executable: Python executable for serve-mcp, e.g.
        ``"/path/to/.venv/bin/python"``. ``None`` uses
        :data:`sys.executable`.
    :returns: TOML text for ``[mcp_servers.omnigent]``.
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
    return f"[mcp_servers.omnigent]\ncommand = {json.dumps(python)}\nargs = [{args_toml}]\n"


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
    pin_line = f'model = "{model}"'
    lines = existing.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("["):
            break
        if re.match(r"^model\s*=", line):
            lines[i] = pin_line
            replaced = True
            break
    if not replaced:
        lines.insert(0, pin_line)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _inject_mcp_server_config(
    codex_home: Path,
    bridge_dir: Path,
    python_executable: str | None = None,
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
    section = _codex_mcp_server_config_section(bridge_dir, python_executable)
    rendered = f"{updated}\n\n{section}" if updated else section
    config_path.write_text(rendered, encoding="utf-8")


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
        self._ws: Any | None = None
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
        :raises RuntimeError: If the app-server returns an error.
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
            raise RuntimeError(str(error))
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
            message = json.loads(raw)
            if (
                "id" in message
                and "method" not in message
                and ("result" in message or "error" in message)
            ):
                future = self._pending_requests.pop(int(message["id"]), None)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            await self._events.put(message)


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
        _populate_codex_home_config(
            self.codex_home,
            _codex_home_config_source_from_env(),
        )
        # Write the MCP server config into config.toml so the app-server
        # discovers it at config load. The -c overrides may not be honored
        # by `codex app-server`, so we write directly to the file.
        _inject_mcp_server_config(self.codex_home, self.bridge_dir, self.python_executable)
        if self.pinned_model:
            _pin_codex_config_model(self.codex_home, self.pinned_model)
        # Native policy enforcement needs codex's hook-trust protocol
        # (``currentHash`` / ``trustStatus`` in ``hooks/list``), added in
        # codex 0.129. Below that the hook can never be trusted, so
        # registering it would only fail at the trust gate. Detect the
        # version up front; below the minimum we skip registration and
        # degrade to "no enforcement" with a surfaced reason. A version we
        # cannot parse (``None``) is treated as supported so a flaky probe
        # never silently disables enforcement — a genuine trust failure is
        # then caught below.
        codex_version = await _codex_cli_version(self.codex_path)
        if codex_version is not None and codex_version < _MIN_POLICY_HOOK_CODEX_VERSION:
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
                self.codex_home, self.bridge_dir, self.python_executable
            )
            if self.ap_server_url:
                write_policy_hook_config(
                    self.bridge_dir,
                    ap_server_url=self.ap_server_url,
                    ap_auth_headers=self.ap_auth_headers or {},
                )
        resolved_listen = self.listen_url or f"unix://{self.socket_path}"
        argv = [
            self.codex_path,
            "app-server",
            "--listen",
            resolved_listen,
        ]
        for override in self.config_overrides:
            argv.extend(["-c", override])
        proc_env = {**self.env, "CODEX_HOME": str(self.codex_home)}
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            cwd=str(self.cwd),
            start_new_session=(os.name == "posix"),
        )
        self.recent_stderr = []
        self.stderr_task = asyncio.create_task(
            self._stderr_loop(),
            name="codex-native-app-server-stderr",
        )
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
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.stderr_task
        self.proc = None
        self.stderr_task = None

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

    :param bridge_dir: Native Codex bridge directory passed to the hook
        via ``--bridge-dir``.
    :param python_executable: Python executable to run, e.g.
        ``"/path/to/python"``. ``None`` uses :data:`sys.executable`.
    :returns: A shell-escaped command string, e.g.
        ``"/path/python -m omnigent.codex_native_hook evaluate-policy
        --bridge-dir /home/u/.omnigent/codex-native/abc"``.
    """
    python = python_executable or sys.executable
    return shlex.join(
        [python, "-m", _POLICY_HOOK_MODULE, "evaluate-policy", "--bridge-dir", str(bridge_dir)]
    )


def _codex_policy_hooks_settings(
    bridge_dir: Path, python_executable: str | None
) -> dict[str, Any]:
    """
    Build the ``hooks.json`` payload registering the policy hook.

    Registers one catch-all (no ``matcher``) command hook on both
    ``PreToolUse`` (blocks before execution) and ``PostToolUse`` (warns
    after). ``mcp__*`` tools are filtered out inside the hook itself, not
    by a matcher, so the relay path remains the single MCP enforcement
    point.

    :param bridge_dir: Native Codex bridge directory.
    :param python_executable: Python executable for the hook command.
    :returns: A ``hooks.json``-shaped dict.
    """
    hook = {
        "type": "command",
        "command": _codex_policy_hook_command(bridge_dir, python_executable),
        "timeout": _POLICY_HOOK_TIMEOUT_SECONDS,
    }
    return {"hooks": {"PreToolUse": [{"hooks": [hook]}], "PostToolUse": [{"hooks": [hook]}]}}


def _write_codex_policy_hooks_file(
    codex_home: Path, bridge_dir: Path, python_executable: str | None
) -> None:
    """
    Write ``hooks.json`` into the private CODEX_HOME (atomically).

    :param codex_home: Private per-session ``CODEX_HOME`` directory.
    :param bridge_dir: Native Codex bridge directory for the hook command.
    :param python_executable: Python executable for the hook command.
    :returns: None.
    """
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = codex_home / _CODEX_HOOKS_FILE
    payload = _codex_policy_hooks_settings(bridge_dir, python_executable)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_CODEX_HOOKS_FILE}.", dir=str(codex_home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _our_policy_hooks_from_list(listed: dict[str, Any], cwd: str) -> list[dict[str, Any]]:
    """
    Extract *our* policy hooks for *cwd* from a ``hooks/list`` response.

    Filters to hooks whose command references :data:`_POLICY_HOOK_MODULE`
    so the trust step never touches hooks the user's symlinked
    ``config.toml`` might declare.

    :param listed: Parsed ``hooks/list`` response envelope, with
        ``result.data`` a list of ``{cwd, hooks: [...]}`` entries.
    :param cwd: The cwd whose hook set to read, e.g.
        ``"/home/user/repo"``.
    :returns: The matching Omnigent hook metadata dicts (possibly
        empty), each with ``key``, ``currentHash``, ``trustStatus``.
    """
    result = listed.get("result", listed)
    data = result.get("data", []) if isinstance(result, dict) else []
    for entry in data:
        if isinstance(entry, dict) and entry.get("cwd") == cwd:
            hooks = entry.get("hooks", [])
            return [
                h
                for h in hooks
                if isinstance(h, dict) and _POLICY_HOOK_MODULE in str(h.get("command", ""))
            ]
    return []


def _hooks_list_diagnostics(listed: dict[str, Any], cwd: str) -> str:
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
    result = listed.get("result", listed)
    data = result.get("data", []) if isinstance(result, dict) else []
    entries = [e for e in data if isinstance(e, dict)]
    if not entries or all(not e.get("hooks") for e in entries):
        return (
            "hooks/list returned no hooks (codex loaded none — likely an "
            "invalid per-session config.toml, so codex fell back to defaults)"
        )
    matched_cwd = any(e.get("cwd") == cwd for e in entries)
    parts: list[str] = []
    for entry in entries:
        hooks = entry.get("hooks", []) or []
        ours = sum(
            1
            for h in hooks
            if isinstance(h, dict) and _POLICY_HOOK_MODULE in str(h.get("command", ""))
        )
        parts.append(f"cwd={entry.get('cwd')!r}: {len(hooks)} hook(s), {ours} ours")
    prefix = "" if matched_cwd else f"no entry matched queried cwd {cwd!r}; "
    return f"hooks/list returned [{prefix}{'; '.join(parts)}]"


def _untrusted_hook_detail(hooks: list[dict[str, Any]]) -> str:
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
    trust_value = {
        str(h["key"]): {"trusted_hash": h["currentHash"]}
        for h in untrusted
        if h.get("key") and h.get("currentHash")
    }
    await client.request(
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
) -> CodexNativeAppServer:
    """
    Build a configured native Codex app-server process wrapper.

    :param socket_path: Unix socket path for the app-server.
    :param codex_home: Private per-session ``CODEX_HOME`` path.
    :param cwd: Working directory for Codex, e.g. the user's repo.
    :param model: Optional Codex model id, e.g. ``"gpt-5.5"``.
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
    :returns: Configured app-server process wrapper.
    :raises ImportError: If no Codex CLI is available.
    :raises OSError: If Databricks routing was requested but no
        credentials can be resolved.
    """
    resolved_codex = codex_path or _find_codex_cli()
    if not resolved_codex:
        raise ImportError("Native Codex requires the 'codex' CLI on PATH.")
    env = _clean_codex_env()
    config_overrides: list[str] = [_CODEX_DEFAULT_REASONING_CONFIG]
    if profile is not None:
        creds = _read_databrickscfg(profile)
        host = creds.host if creds is not None else _read_databrickscfg_host(profile)
        if not host:
            raise OSError(
                f"Native Codex with Databricks profile {profile!r} (from your "
                "provider config) requires a matching ~/.databrickscfg section "
                "with a host visible to the runner process."
            )
        host = host.rstrip("/")
        config_overrides.extend(
            _databricks_codex_config_overrides(
                model=model or _DATABRICKS_CODEX_DEFAULT_MODEL,
                base_url=_databricks_codex_base_url(host),
                auth_command=_databricks_codex_auth_command(host, profile),
            )
        )
        env["DATABRICKS_HOST"] = host
    if extra_config_overrides:
        config_overrides.extend(extra_config_overrides)
    return CodexNativeAppServer(
        codex_path=resolved_codex,
        socket_path=socket_path,
        codex_home=codex_home,
        env=env,
        config_overrides=config_overrides,
        cwd=cwd,
        bridge_dir=bridge_dir,
        ap_server_url=ap_server_url,
        ap_auth_headers=ap_auth_headers,
        python_executable=python_executable,
        pinned_model=model,
    )


@dataclass(frozen=True)
class NativeCodexLaunch:
    """How a native Codex terminal should be launched, across all offerings.

    Resolved by :func:`resolve_native_codex_launch` so a native Codex
    session honors ``configure harnesses`` like the in-process codex harness.

    :param config_overrides: Codex ``-c`` overrides that route through a
        generic provider (``model_provider`` + base_url + auth + wire);
        empty for the Databricks-profile and CLI-login paths.
    :param model: Model id to pin, or ``None`` when the caller has not yet
        resolved a provider-specific default.
    :param profile: Databricks profile for the ucode path, or ``None`` (a
        generic provider routes via *config_overrides*; CLI login uses
        neither).
    """

    config_overrides: list[str]
    model: str | None
    profile: str | None


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
            return json.loads(override.removeprefix(prefix))
    if launch.profile is not None:
        return "omnigent_databricks"
    return "openai"


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
        provider's default model, e.g. ``"gpt-5.5"``; ``None`` selects the
        provider/Codex default.
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
            model=model or _DATABRICKS_CODEX_DEFAULT_MODEL,
            profile=entry.profile,
        )
    if entry.kind == CLI_CONFIG_KIND:
        # Pin the config.toml-defined provider by name; its table (and
        # credential) ride along via the bridged config.toml. json.dumps
        # yields a valid TOML basic string for the -c override value.
        return NativeCodexLaunch(
            config_overrides=[f"model_provider={json.dumps(entry.model_provider)}"],
            model=model or _OPENAI_CODEX_DEFAULT_MODEL,
            profile=None,
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
    return NativeCodexLaunch(config_overrides=overrides, model=pinned, profile=None)


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
    pinned = model or _OPENAI_CODEX_DEFAULT_MODEL
    if codex_auth_has_credential(real_codex_home / "auth.json"):
        _logger.info(
            "native-codex routing: Codex CLI login (subscription provider %r; Codex is logged in)",
            entry.name,
        )
        return NativeCodexLaunch(
            config_overrides=subscription_overrides, model=pinned, profile=None
        )
    fallback = _first_routable_codex_provider(explicit, exclude=entry.name, model=model)
    if fallback is not None:
        return fallback
    _logger.info(
        "native-codex routing: Codex CLI login (subscription provider %r has no usable "
        "Codex login and no alternative provider is configured)",
        entry.name,
    )
    return NativeCodexLaunch(config_overrides=subscription_overrides, model=pinned, profile=None)


def resolve_native_codex_launch(*, model: str | None) -> NativeCodexLaunch:
    """Resolve the native Codex launch config across all offerings.

    Mirrors the in-process codex harness routing precedence
    (:func:`omnigent.runtime.workflow._resolve_provider_for_build`) for the
    ``openai`` surface, so ``omnigent codex`` and a host-spawned native
    Codex session route through ``omnigent setup``:

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

    Credentials are controlled exclusively by ``omnigent setup``
    provider config (or the legacy global ``auth:`` block) — there is
    no CLI/env profile override.

    :param model: An explicit/session model override that wins over the
        provider's default model, or ``None``.
    :returns: The resolved :class:`NativeCodexLaunch`.
    """
    from omnigent.onboarding.detected import (
        codex_config_provider_dismissed,
        effective_config_with_detected,
    )
    from omnigent.onboarding.provider_config import (
        SUBSCRIPTION_KIND,
        default_provider_for_harness,
        load_config,
    )
    from omnigent.runtime.workflow import _load_global_auth
    from omnigent.spec.types import DatabricksAuth

    explicit = load_config()
    # When the launch ends up on codex's own login with NO provider routing,
    # the bridged config.toml's custom default model_provider would still
    # apply — including one the user explicitly Removed (dismissed). Pin
    # codex's built-in provider in that case so the dismissal holds at run
    # time. An undetectable/undismissed custom provider keeps its routing.
    no_provider_overrides = (
        ['model_provider="openai"'] if codex_config_provider_dismissed(explicit) else []
    )
    entry = default_provider_for_harness(explicit, "codex")
    if entry is None:
        # No explicit provider default: global auth wins over ambient
        # (parity with _resolve_provider_for_build).
        global_auth = _load_global_auth()
        if isinstance(global_auth, DatabricksAuth):
            return NativeCodexLaunch(
                config_overrides=[],
                model=model or _DATABRICKS_CODEX_DEFAULT_MODEL,
                profile=global_auth.profile,
            )
        if global_auth is not None:
            return NativeCodexLaunch(
                config_overrides=[], model=model or _OPENAI_CODEX_DEFAULT_MODEL, profile=None
            )
        entry = default_provider_for_harness(effective_config_with_detected(explicit), "codex")

    if entry is None:
        _logger.info(
            "native-codex routing: Codex CLI login (no provider configured for the Codex "
            "harness, no Databricks profile). Run `omnigent setup --no-internal-beta` to route "
            "through a provider."
        )
        return NativeCodexLaunch(
            config_overrides=no_provider_overrides,
            model=model or _OPENAI_CODEX_DEFAULT_MODEL,
            profile=None,
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
        model=model or _OPENAI_CODEX_DEFAULT_MODEL,
        profile=None,
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


async def preload_codex_thread_for_resume(transport: str, thread_id: str) -> None:
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
            {"threadId": thread_id, "excludeTurns": True},
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


def build_codex_remote_args(
    *,
    codex_args: tuple[str, ...],
    thread_id: str | None,
    remote_url: str,
    config_overrides: tuple[str, ...] = (),
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
    :returns: Codex argv tail after the executable.
    """
    override_args: list[str] = []
    for override in config_overrides:
        override_args.extend(["-c", override])
    if thread_id is None:
        return [*override_args, *codex_args, "--remote", remote_url]
    return [*override_args, *codex_args, "resume", "--remote", remote_url, thread_id]


def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """
    Send SIGTERM to a subprocess process group when possible.

    :param process: Subprocess handle to terminate.
    :returns: None.
    """
    if process.returncode is not None:
        return
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGTERM)
            return
    with contextlib.suppress(ProcessLookupError, Exception):
        process.terminate()


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """
    Send SIGKILL to a subprocess process group when possible.

    :param process: Subprocess handle to kill.
    :returns: None.
    """
    if process.returncode is not None:
        return
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
            return
    with contextlib.suppress(ProcessLookupError, Exception):
        process.kill()
