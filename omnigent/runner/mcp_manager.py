"""Runner-side MCP pool. See ``designs/RUNNER_MCP.md``."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.types import ElicitRequestParams, ElicitResult
from mcp.types import Tool as McpToolDef

from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.tools.base import is_valid_tool_name
from omnigent.tools.mcp import McpServerConnection

_logger = logging.getLogger(__name__)


def _build_accept_content(
    params: ElicitRequestParams,
) -> dict[str, str | int | float | bool | list[str] | None] | None:
    """
    Auto-fill ``content`` from ``requestedSchema`` for an accept.

    Delegates to the shared utility in
    :func:`omnigent.tools._elicitation_schema.build_accept_content_from_schema`.

    :param params: The elicitation params from the MCP server.
    :returns: A flat content dict, or ``None``.
    """
    from omnigent.tools._elicitation_schema import build_accept_content_from_schema

    schema = getattr(params, "requestedSchema", None)
    if not schema or not isinstance(schema, dict):
        return None
    return build_accept_content_from_schema(schema)


_POOL_SPEC_CAPACITY = 8


@dataclass
class _SharedServerEntry:
    """One live MCP server connection shared by any spec with the same config."""

    server_hash: str
    config: MCPServerConfig
    connection: McpServerConnection | None = None
    tools: list[McpToolDef] = field(default_factory=list)
    error: str | None = None
    ref_count: int = 0
    connect_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass
class _SpecServerRef:
    """A spec-specific name/filter pointing at a shared MCP server."""

    config: MCPServerConfig
    server_hash: str
    entry: _SharedServerEntry


@dataclass
class _SpecEntry:
    spec_hash: str
    servers: dict[str, _SpecServerRef] = field(default_factory=dict)
    server_hashes: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class McpSchemasResult:
    """Output of :meth:`RunnerMcpManager.schemas_for`."""

    schemas: list[dict[str, Any]]
    tool_names: set[str]
    failures: dict[str, str]  # server_name → error message


def compute_spec_hash(configs: list[MCPServerConfig], cwd: Path | None = None) -> str:
    """Stable content hash over ``spec.mcp_servers`` (+ stdio cwd)."""
    payload = json.dumps(
        {
            "cwd": str(cwd) if cwd is not None else None,
            "servers": [
                {
                    "name": c.name,
                    "transport": c.transport,
                    "url": c.url,
                    "headers": dict(c.headers or {}),
                    "databricks_profile": c.databricks_profile,
                    "command": c.command,
                    "args": list(c.args or []),
                    "env": dict(c.env or {}),
                    "tools": list(getattr(c, "tools", None) or []),
                    "timeout": c.timeout,
                    "retry": _retry_payload(c.retry),
                }
                for c in configs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _retry_payload(retry: Any | None) -> Any:
    """Return a stable JSON payload for a retry policy-like object."""
    if retry is None:
        return None
    to_json = getattr(retry, "to_json", None)
    if callable(to_json):
        return json.loads(to_json())
    return repr(retry)


def compute_server_hash(config: MCPServerConfig, cwd: Path | None = None) -> str:
    """Stable content hash over fields that determine one MCP connection.

    ``name`` and ``tools`` are intentionally excluded: two specs can expose
    the same underlying server with different namespaces or allow-lists while
    sharing one transport/subprocess.
    """
    payload = json.dumps(
        {
            "cwd": str(cwd) if config.transport == "stdio" and cwd is not None else None,
            "transport": config.transport,
            "url": config.url,
            "headers": dict(config.headers or {}),
            "databricks_profile": config.databricks_profile,
            "command": config.command,
            "args": list(config.args or []),
            "env": dict(config.env or {}),
            "timeout": config.timeout,
            "retry": _retry_payload(config.retry),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _mcp_tool_schema(
    server_name: str,
    tool_def: McpToolDef,
    allowed: set[str] | None,
) -> dict[str, Any] | None:
    """Translate an MCP tool def to an OpenAI function-tool schema with a
    namespaced name; honor *allowed*.

    Tool names are returned as ``{server_name}__{tool_def.name}`` (double
    underscore separator) so tools from different MCP servers never collide,
    even when two servers expose a tool with the same bare name (e.g.
    ``search``).  The caller is responsible for stripping the prefix before
    dispatching to the MCP server (see ``RunnerMcpManager.call_tool``).

    Returns ``None`` when the tool is filtered out by *allowed* or the bare
    tool name is invalid (must match ``^[a-zA-Z0-9_-]{1,256}$``).

    :param server_name: Config name of the MCP server, e.g. ``"github"``.
    :param tool_def: The raw tool definition returned by the MCP server.
    :param allowed: Optional allowlist of **bare** tool names (as declared
        in the spec's ``tools:`` list).  ``None`` means all tools are
        allowed.
    :returns: OpenAI function-tool schema dict with a namespaced ``name``,
        or ``None`` when the tool is filtered or has an invalid bare name.
    """
    from omnigent.tools.mcp import _normalize_input_schema

    bare_name = tool_def.name
    # Check allowed-list and name validity against the bare name, before
    # constructing the namespaced version, so spec authors write plain tool
    # names in their YAML (e.g. ``tools: [search]``, not ``github__search``).
    if allowed is not None and bare_name not in allowed:
        return None
    if not is_valid_tool_name(bare_name):
        _logger.warning(
            "MCP tool %r from server %r has an invalid name "
            "(must match [a-zA-Z0-9_-]{1,256}) — skipping",
            bare_name,
            server_name,
        )
        return None
    # Namespace: ``{server_name}__{bare_name}`` so two servers with a tool
    # named ``search`` produce ``github__search`` and ``glean__search``.
    namespaced_name = f"{server_name}__{bare_name}"
    return {
        "type": "function",
        "name": namespaced_name,
        "description": tool_def.description or "",
        "parameters": _normalize_input_schema(tool_def.inputSchema, namespaced_name),
    }


class RunnerMcpManager:
    """Per-runner MCP pool. Async methods run on the runner's loop.

    :param stdio_cwd: Working directory for spawned stdio MCP
        subprocesses. Defaults to ``None`` (subprocess inherits the
        runner's cwd). The CLI passes the user's project root here
        so relative ``command: .venv/bin/python`` resolves correctly
        when the runner itself is launched from a different cwd.
    """

    def __init__(
        self,
        stdio_cwd: Path | None = None,
        server_client: Any | None = None,
    ) -> None:
        """
        :param stdio_cwd: Working directory for spawned stdio MCP
            subprocesses.
        :param server_client: ``httpx.AsyncClient`` pointed at the
            Omnigent server. When provided, inline MCP elicitations are
            surfaced to the user via the Omnigent server's session events
            API. When ``None``, inline elicitations are declined.
        """
        self._specs: dict[str, _SpecEntry] = {}
        self._servers: dict[str, _SharedServerEntry] = {}
        self._lru: list[str] = []  # most-recent at end
        self._lock = asyncio.Lock()
        # Hold strong refs to fire-and-forget eviction-close tasks so
        # the GC doesn't cancel them mid-flight (RUF006).
        self._evict_tasks: set[asyncio.Task[None]] = set()
        self._stdio_cwd = stdio_cwd
        self._server_client = server_client

    def _build_elicitation_callback(
        self,
    ) -> Callable[[str, ElicitRequestParams], Awaitable[ElicitResult]]:
        """
        Build an inline elicitation callback for MCP connections.

        When ``server_client`` is available, surfaces the
        elicitation to the user via the Omnigent server's session events
        API (approval card in web UI, y/a/n prompt in REPL) and
        parks until the user responds. Falls back to decline
        when no Omnigent server is available.

        :returns: Async callback ``(session_id, params) →
            ElicitResult``.
        """
        server_client = self._server_client

        async def _elicit(
            session_id: str,
            params: ElicitRequestParams,
        ) -> ElicitResult:
            """
            Handle an inline ``elicitation/create`` from the MCP
            server.

            When an Omnigent server client is available, POSTs a
            ``mcp_elicitation`` event to surface the approval
            prompt and parks on ``pending_approvals``. Otherwise
            declines.

            :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
            :param params: MCP elicitation params from the gateway.
            :returns: User verdict as an :class:`ElicitResult`.
            """
            if server_client is None:
                _logger.warning(
                    "MCP elicitation callback: no Omnigent server client available — declining",
                )
                return ElicitResult(action="decline")

            from omnigent.runner import pending_approvals

            message = getattr(params, "message", "")
            requested_schema = getattr(params, "requestedSchema", None)
            body: dict[str, Any] = {
                "type": "mcp_elicitation",
                "data": {"message": message},
            }
            if requested_schema is not None:
                body["data"]["requestedSchema"] = requested_schema

            try:
                resp = await server_client.post(
                    f"/v1/sessions/{session_id}/events",
                    json=body,
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "MCP elicitation callback: Omnigent server POST failed (%s) — declining",
                    exc,
                )
                return ElicitResult(action="decline")

            elicitation_id = data.get("elicitation_id", "")
            if not elicitation_id:
                _logger.warning(
                    "MCP elicitation callback: Omnigent server returned no "
                    "elicitation_id — declining",
                )
                return ElicitResult(action="decline")

            # Park until the user approves or declines (or timeout).
            # ``pending_approvals`` resolves a bool (accept/decline)
            # — it does not carry the user's form data. Content is
            # auto-filled from the requestedSchema below.
            # No-op publish_event: ``response.elicitation_resolved``
            # won't fire on timeout/cancellation, so the Omnigent server's
            # sidebar badge may stay stale. Same pattern as
            # proxy_mcp_manager. A future enhancement could POST
            # the resolved event back to the Omnigent server here.
            approved = await pending_approvals.wait_for_user_approval(
                elicitation_id=elicitation_id,
                conversation_id=session_id,
                publish_event=lambda _s, _e: None,
            )

            if not approved:
                return ElicitResult(action="decline")

            content = _build_accept_content(params)
            return ElicitResult(action="accept", content=content)

        return _elicit

    async def prewarm(self, spec: AgentSpec) -> None:
        """Register *spec*'s MCPs without spawning transports.

        This keeps runner startup cheap when a spec lists many MCPs; the
        first schema lookup or tool call still pays the server cold-start.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            return
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        async with self._lock:
            self._ensure_entry(spec_hash, configs)
            self._touch(spec_hash)

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        """Resolve MCP schemas for *spec*; awaits any in-flight connect."""
        configs = list(spec.mcp_servers or [])
        if not configs:
            return McpSchemasResult(schemas=[], tool_names=set(), failures={})
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        async with self._lock:
            entry = self._ensure_entry(spec_hash, configs)
            self._touch(spec_hash)
            refs = list(entry.servers.values())
            for ref in refs:
                self._retain_server_ref(ref.entry)
            connect_tasks = {
                ref.server_hash: task
                for ref in refs
                if (task := self._ensure_connect_task(ref.entry, entry.spec_hash)) is not None
            }

        try:
            if connect_tasks:
                try:
                    await asyncio.gather(*connect_tasks.values())
                except Exception:
                    _logger.exception("runner mcp connect task raised; surfacing partial results")

            schemas: list[dict[str, Any]] = []
            tool_names: set[str] = set()
            failures: dict[str, str] = {}
            for ref in refs:
                server = ref.entry
                if server.error is not None:
                    failures[ref.config.name] = server.error
                    continue
                allowed = self._allowed_tools(ref)
                for td in server.tools:
                    schema = _mcp_tool_schema(ref.config.name, td, allowed)
                    if schema is None:
                        continue
                    schemas.append(schema)
                    tool_names.add(schema["name"])
            return McpSchemasResult(schemas=schemas, tool_names=tool_names, failures=failures)
        finally:
            async with self._lock:
                for ref in refs:
                    self._release_server_ref(ref.entry, entry.spec_hash)

    async def call_tool(
        self,
        spec: AgentSpec,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
    ) -> str:
        """
        Dispatch *tool_name* against the pool's cached MCP session.

        :param spec: Agent spec whose MCP servers to dispatch against.
        :param tool_name: Namespaced tool name, e.g.
            ``"github__list_issues"``.
        :param arguments: Decoded tool argument dict.
        :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
            Forwarded to the connection for inline elicitation
            context. ``None`` when no session is available.
        :returns: Tool result string.
        :raises McpElicitationRequired: When the MCP server returns
            an ``InputRequiredResult`` requiring user input before
            the tool can execute.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            raise RuntimeError(
                f"runner has no MCPs registered for this spec; cannot dispatch {tool_name!r}"
            )
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        server_to_release: _SharedServerEntry | None = None
        try:
            if "__" in tool_name:
                async with self._lock:
                    entry = self._ensure_entry(spec_hash, configs)
                    self._touch(spec_hash)
                    route_ref = self._resolve_tool_ref(entry, tool_name)
                    if route_ref is None:
                        raise RuntimeError(f"runner has no live MCP serving tool {tool_name!r}")
                    ref, bare_name = route_ref
                    self._retain_server_ref(ref.entry)
                    server_to_release = ref.entry
                    connect_task = self._ensure_connect_task(ref.entry, entry.spec_hash)

                if connect_task is not None:
                    await connect_task
                if (
                    ref.entry.error is None
                    and ref.entry.connection is not None
                    and self._server_has_allowed_tool(ref, bare_name)
                ):
                    route = (ref.entry, bare_name)
                else:
                    route = None
            else:
                await self.schemas_for(spec)
                async with self._lock:
                    entry = self._specs.get(spec_hash)
                    route = (
                        None
                        if entry is None
                        else self._resolve_tool_route_from_entry(entry, tool_name)
                    )
                    if route is not None:
                        self._retain_server_ref(route[0])
                        server_to_release = route[0]

            if route is None:
                raise RuntimeError(f"runner has no live MCP serving tool {tool_name!r}")
            owning_server, bare_name = route
            if owning_server.connection is None:
                raise RuntimeError(f"runner has no live MCP serving tool {tool_name!r}")

            return await owning_server.connection.call_tool(
                bare_name,
                arguments,
                session_id=session_id,
            )
        finally:
            if server_to_release is not None:
                async with self._lock:
                    self._release_server_ref(server_to_release, spec_hash)

    def _resolve_tool_route(
        self,
        spec: AgentSpec,
        tool_name: str,
    ) -> tuple[_SharedServerEntry, str] | None:
        """
        Find the live server and bare MCP tool name for *tool_name*.

        Namespaced names must match their server prefix exactly. Bare names
        are accepted only for internal/test callers.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            return None
        spec_hash = compute_spec_hash(configs, self._stdio_cwd)
        entry = self._specs.get(spec_hash)
        if entry is None:
            return None
        return self._resolve_tool_route_from_entry(entry, tool_name)

    def _resolve_tool_route_from_entry(
        self,
        entry: _SpecEntry,
        tool_name: str,
    ) -> tuple[_SharedServerEntry, str] | None:
        """Find a connected, allowed MCP tool route inside *entry*."""
        if "__" in tool_name:
            route_ref = self._resolve_tool_ref(entry, tool_name)
            if route_ref is None:
                return None
            ref, bare_tool = route_ref
            if ref.entry.error is not None:
                return None
            if self._server_has_allowed_tool(ref, bare_tool):
                return ref.entry, bare_tool
            return None

        for ref in self._ordered_server_refs(entry):
            server = ref.entry
            if server.error is not None:
                continue
            if self._server_has_allowed_tool(ref, tool_name):
                return server, tool_name
        return None

    @staticmethod
    def _ordered_server_refs(entry: _SpecEntry) -> list[_SpecServerRef]:
        """Prefer the longest namespace when server names overlap."""
        return sorted(entry.servers.values(), key=lambda ref: len(ref.config.name), reverse=True)

    def _resolve_tool_ref(
        self,
        entry: _SpecEntry,
        tool_name: str,
    ) -> tuple[_SpecServerRef, str] | None:
        """Find the spec ref addressed by a namespaced tool name."""
        if "__" not in tool_name:
            return None
        for ref in self._ordered_server_refs(entry):
            prefix = f"{ref.config.name}__"
            if not tool_name.startswith(prefix):
                continue
            bare_name = tool_name[len(prefix) :]
            if not self._is_tool_allowed(ref, bare_name) or not is_valid_tool_name(bare_name):
                return None
            return ref, bare_name
        return None

    @staticmethod
    def _allowed_tools(ref: _SpecServerRef) -> set[str] | None:
        tools = getattr(ref.config, "tools", None)
        return set(tools) if tools else None

    def _is_tool_allowed(self, ref: _SpecServerRef, bare_name: str) -> bool:
        allowed = self._allowed_tools(ref)
        return allowed is None or bare_name in allowed

    def _server_has_allowed_tool(self, ref: _SpecServerRef, bare_name: str) -> bool:
        return (
            self._is_tool_allowed(ref, bare_name)
            and is_valid_tool_name(bare_name)
            and any(td.name == bare_name for td in ref.entry.tools)
        )

    def _resolve_owning_server(
        self,
        spec: AgentSpec,
        tool_name: str,
    ) -> _SharedServerEntry | None:
        """
        Find the server entry that owns *tool_name*.

        Used by the MRTR retry path in ``/mcp/execute`` to access
        the ``McpServerConnection`` directly for
        ``call_tool_with_elicitation``.

        :param spec: Agent spec whose MCP servers to search.
        :param tool_name: Namespaced or bare MCP tool name.
        :returns: The owning shared server entry, or ``None`` if the
            tool is not found.
        """
        route = self._resolve_tool_route(spec, tool_name)
        return None if route is None else route[0]

    async def shutdown(self) -> None:
        """Best-effort close of every active MCP connection."""
        async with self._lock:
            servers = list(self._servers.values())
            connect_tasks = [
                task
                for server in servers
                if (task := server.connect_task) is not None and not task.done()
            ]
            for server in servers:
                server.ref_count = 0
            self._specs.clear()
            self._servers.clear()
            self._lru.clear()
            for task in connect_tasks:
                task.cancel()

        if connect_tasks:
            await asyncio.gather(*connect_tasks, return_exceptions=True)

        for server in servers:
            conn = server.connection
            if conn is None:
                continue
            server.connection = None
            server.tools = []
            try:
                await conn.close()
            except Exception:
                _logger.exception(
                    "error closing MCP %r (%s) during shutdown",
                    server.config.name,
                    server.server_hash,
                )

        if self._evict_tasks:
            await asyncio.gather(*list(self._evict_tasks), return_exceptions=True)

    def _ensure_entry(self, spec_hash: str, configs: list[MCPServerConfig]) -> _SpecEntry:
        """Return or create the pool entry for *spec_hash*. Caller holds lock."""
        entry = self._specs.get(spec_hash)
        if entry is not None:
            return entry
        entry = _SpecEntry(spec_hash=spec_hash)
        for cfg in configs:
            server_hash = compute_server_hash(cfg, self._stdio_cwd)
            server = self._servers.get(server_hash)
            if server is None:
                server = _SharedServerEntry(server_hash=server_hash, config=cfg)
                self._servers[server_hash] = server
            if server_hash not in entry.server_hashes:
                server.ref_count += 1
                entry.server_hashes.add(server_hash)
            entry.servers[cfg.name] = _SpecServerRef(
                config=cfg,
                server_hash=server_hash,
                entry=server,
            )
        self._specs[spec_hash] = entry
        self._lru.append(spec_hash)
        self._evict_if_needed()
        return entry

    def _touch(self, spec_hash: str) -> None:
        """Mark *spec_hash* most-recently used. Caller holds lock."""
        with contextlib.suppress(ValueError):
            self._lru.remove(spec_hash)
        self._lru.append(spec_hash)

    def _evict_if_needed(self) -> None:
        """LRU-evict over-capacity entries. Caller holds lock."""
        while len(self._lru) > _POOL_SPEC_CAPACITY:
            victim = self._lru.pop(0)
            entry = self._specs.pop(victim, None)
            if entry is None:
                continue
            _logger.info(
                "runner mcp pool evicting spec %s (over capacity %d)",
                victim,
                _POOL_SPEC_CAPACITY,
            )
            self._release_spec_entry(victim, entry)

    def _release_spec_entry(self, spec_hash: str, entry: _SpecEntry) -> None:
        """Release one spec entry and close shared servers no longer referenced."""
        for server_hash in entry.server_hashes:
            server = self._servers.get(server_hash)
            if server is not None:
                self._release_server_ref(server, spec_hash)

    @staticmethod
    async def _safe_close(conn: McpServerConnection, owner: str, name: str) -> None:
        try:
            await conn.close()
        except Exception:
            _logger.exception("error closing MCP %r for %s", name, owner)

    def _schedule_close(self, conn: McpServerConnection, owner: str, name: str) -> None:
        task = asyncio.create_task(
            self._safe_close(conn, owner, name),
            name=f"runner-mcp-close:{owner}:{name}",
        )
        self._evict_tasks.add(task)
        task.add_done_callback(self._evict_tasks.discard)

    @staticmethod
    def _retain_server_ref(server: _SharedServerEntry) -> None:
        server.ref_count += 1

    def _release_server_ref(self, server: _SharedServerEntry, owner: str) -> None:
        """Release one server ref. Caller holds ``self._lock``."""
        if server.ref_count <= 0:
            return
        server.ref_count -= 1
        if server.ref_count > 0:
            return
        if server.connect_task is not None and not server.connect_task.done():
            return

        self._servers.pop(server.server_hash, None)
        conn = server.connection
        server.connection = None
        server.tools = []
        if conn is not None:
            self._schedule_close(conn, owner, server.config.name)

    def _ensure_connect_task(
        self,
        server: _SharedServerEntry,
        spec_hash: str,
    ) -> asyncio.Task[None] | None:
        """Return an in-flight shared connect task, creating one if needed."""
        if server.connection is not None:
            return None
        if server.connect_task is None or server.connect_task.done():
            server.connect_task = asyncio.create_task(
                self._connect_server(server, spec_hash),
                name=f"runner-mcp-connect:{server.server_hash}",
            )
        return server.connect_task

    async def _connect_server(self, server: _SharedServerEntry, spec_hash: str) -> None:
        """Connect one shared MCP server if needed."""
        close_after_connect: McpServerConnection | None = None
        current_task = asyncio.current_task()
        try:
            async with server.lock:
                if server.connection is not None:
                    return

                conn = McpServerConnection(
                    config=server.config,
                    cwd=self._stdio_cwd,
                    elicitation_callback=self._build_elicitation_callback(),
                )
                try:
                    tools = await conn.connect()
                except asyncio.CancelledError:
                    await self._safe_close(conn, spec_hash, server.config.name)
                    raise
                except Exception as exc:  # noqa: BLE001
                    async with self._lock:
                        server.error = f"{type(exc).__name__}: {exc}"
                        server.connection = None
                        server.tools = []
                    _logger.warning(
                        "runner mcp connect failed: spec=%s server_hash=%s server=%s error=%s",
                        spec_hash,
                        server.server_hash,
                        server.config.name,
                        server.error,
                    )
                    return

                async with self._lock:
                    server.connection = conn
                    server.tools = tools
                    server.error = None
                    if server.ref_count <= 0:
                        self._servers.pop(server.server_hash, None)
                        server.connection = None
                        server.tools = []
                        close_after_connect = conn

                _logger.info(
                    "runner mcp connected: spec=%s server_hash=%s server=%s tools=%d",
                    spec_hash,
                    server.server_hash,
                    server.config.name,
                    len(tools),
                )
        finally:
            cleanup_conn: McpServerConnection | None = None
            async with self._lock:
                if server.connect_task is current_task:
                    server.connect_task = None
                if server.ref_count <= 0:
                    self._servers.pop(server.server_hash, None)
                    if server.connection is not None:
                        cleanup_conn = server.connection
                        server.connection = None
                        server.tools = []

            if cleanup_conn is not None and cleanup_conn is not close_after_connect:
                await self._safe_close(cleanup_conn, spec_hash, server.config.name)

        if close_after_connect is not None:
            await self._safe_close(close_after_connect, spec_hash, server.config.name)

    def status_snapshot(self) -> dict[str, Any]:
        """JSON-able view of pool state for introspection."""
        out_specs: list[dict[str, Any]] = []
        for spec_hash in self._lru:
            entry = self._specs.get(spec_hash)
            if entry is None:
                continue
            out_specs.append(
                {
                    "spec_hash": spec_hash,
                    "servers": [
                        {
                            "name": s.config.name,
                            "status": "ready"
                            if s.entry.connection is not None and s.entry.error is None
                            else ("failed" if s.entry.error else "pending"),
                            "tools": [t.name for t in s.entry.tools],
                            "error": s.entry.error,
                        }
                        for s in entry.servers.values()
                    ],
                }
            )
        return {"specs": out_specs}
