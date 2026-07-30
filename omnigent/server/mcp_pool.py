"""AP-server-side MCP connection pool.

Provides :class:`ServerMcpPool` — an AP-server-owned MCP connection
manager analogous to
:class:`omnigent.runner.mcp_manager.RunnerMcpManager` but used by the
``POST /v1/sessions/{session_id}/mcp`` MCP-proxy endpoint to proxy calls
with policy enforcement.

Reuses :class:`omnigent.tools.mcp.McpServerConnection` for transport
and :func:`omnigent.runner.mcp_manager.compute_spec_hash` for stable
pool keying.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp.types import Tool as McpToolDef

from omnigent.inner.datamodel import CredentialBrokerSpec
from omnigent.runner.mcp_manager import (  # noqa: F401 — McpSchemasResult re-exported for callers
    McpSchemasResult,
    _agent_credential_broker,
    compute_spec_hash,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.tools.base import is_valid_tool_name
from omnigent.tools.mcp import McpServerConnection

_logger = logging.getLogger(__name__)

# Maximum number of distinct agents whose connections the pool keeps live.
# When capacity is reached the least-recently-used agent's connections are
# closed and evicted.
_POOL_AGENT_CAPACITY = 32


@dataclass
class _McpServerEntry:
    """One MCP server within a single agent's pool entry.

    :param config: The MCP server configuration from the agent spec,
        e.g. ``MCPServerConfig(name="github", transport="http",
        url="https://mcp.example.com/sse")``.
    :param connection: Live :class:`McpServerConnection`, or ``None``
        before the first successful connect.
    :param tools: MCP tool definitions discovered during ``connect()``.
        Empty until the connection is established.
    :param error: Human-readable error string when the last connect
        attempt failed, e.g. ``"ConnectionRefusedError: ..."``.
        ``None`` when the server is healthy.
    """

    config: MCPServerConfig
    connection: McpServerConnection | None = None
    tools: list[McpToolDef] = field(default_factory=list)
    error: str | None = None


@dataclass
class _AgentEntry:
    """Pool entry for a single agent, keyed by ``agent_id``.

    Multiple sessions may share the same agent; the pool reuses one entry
    across all of them. The entry is invalidated when the spec hash
    changes (agent re-deployed with different MCP servers).

    :param agent_id: The agent this entry belongs to,
        e.g. ``"agent_abc123"``.
    :param spec_hash: Content hash of the agent's ``mcp_servers`` list
        (from :func:`compute_spec_hash`), e.g.
        ``"a1b2c3d4e5f6a7b8"``. Used to detect spec staleness.
    :param servers: Per-server entries keyed by server name
        (``MCPServerConfig.name``), e.g. ``{"github": _McpServerEntry}``.
    :param prewarm_task: Background connect task, or ``None`` when
        not yet started or already completed.
    """

    agent_id: str
    spec_hash: str
    servers: dict[str, _McpServerEntry] = field(default_factory=dict)
    prewarm_task: asyncio.Task[None] | None = None
    credential_broker: CredentialBrokerSpec | None = None


@dataclass(frozen=True)
class McpToolEntry:
    """A single MCP tool paired with its owning server name.

    Returned by :meth:`ServerMcpPool.list_tools`. The caller is
    responsible for namespacing (``server_name__tool_name``) before
    exposing tools in a ``tools/list`` response.

    :param server_name: The MCP server that owns this tool,
        e.g. ``"github"``.
    :param tool: The MCP tool definition including ``name``,
        ``description``, and ``inputSchema``.
    """

    server_name: str
    tool: McpToolDef


class ServerMcpPool:
    """AP-server-side MCP connection pool.

    Manages MCP connections for agents, keyed by ``agent_id``. An entry
    is populated lazily on first use (warm-on-demand). Connections stay
    live for the lifetime of the entry. An LRU eviction policy caps the
    number of live agents at :data:`_POOL_AGENT_CAPACITY`.

    Tool namespacing (``server_name__tool_name``) is **not** applied
    internally — it is the responsibility of callers to namespace names
    in ``tools/list`` responses and to de-namespace before calling
    :meth:`call_tool`.

    Usage::

        pool = ServerMcpPool()

        # List tools for an agent:
        entries = await pool.list_tools(agent_id, spec)
        namespaced = [f"{e.server_name}__{e.tool.name}" for e in entries]

        # Call a tool (de-namespace first):
        result = await pool.call_tool(agent_id, spec, "github", "search", args)

        # Tear down on agent deletion:
        await pool.shutdown_for(agent_id)

        # Tear down everything on server shutdown:
        await pool.shutdown_all()
    """

    def __init__(self) -> None:
        """Initialize an empty pool."""
        self._entries: dict[str, _AgentEntry] = {}
        # Most-recent agent_id at end; front is the eviction candidate.
        self._lru: list[str] = []
        self._lock = asyncio.Lock()
        # Hold strong references to fire-and-forget eviction tasks so
        # the GC does not cancel them mid-flight (RUF006).
        self._evict_tasks: set[asyncio.Task[None]] = set()

    async def list_tools(
        self,
        agent_id: str,
        spec: AgentSpec,
    ) -> list[McpToolEntry]:
        """Return MCP tool definitions for all of the agent's servers.

        Connects to servers if not already warm. Applies the per-server
        ``tools`` allow-list from :class:`MCPServerConfig` when present.

        :param agent_id: The agent whose tools to list,
            e.g. ``"agent_abc123"``.
        :param spec: The agent's spec; reads ``spec.mcp_servers``.
        :returns: A list of :class:`McpToolEntry` objects, one per
            allowed tool per server. Empty when the spec has no
            ``mcp_servers``.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            return []
        await self._ensure_warm(agent_id, configs, _agent_credential_broker(spec))

        async with self._lock:
            entry = self._entries.get(agent_id)
        if entry is None:
            return []

        result: list[McpToolEntry] = []
        for server in entry.servers.values():
            if server.error is not None:
                continue
            allowed: set[str] | None = set(getattr(server.config, "tools", None) or []) or None
            for td in server.tools:
                if not is_valid_tool_name(td.name):
                    _logger.warning(
                        "MCP tool %r from server %r has an invalid name "
                        "(must match [a-zA-Z0-9_-]{1,256}) — skipping",
                        td.name,
                        server.config.name,
                    )
                    continue
                if allowed is not None and td.name not in allowed:
                    continue
                result.append(McpToolEntry(server_name=server.config.name, tool=td))
        return result

    async def call_tool(
        self,
        agent_id: str,
        spec: AgentSpec,
        server_name: str,
        tool_name: str,
        # Values are Any because MCP tool arguments are JSON objects with
        # heterogeneous value types (str, int, bool, nested dicts, etc.).
        arguments: dict[str, Any],
    ) -> str:
        """Invoke a tool on a specific MCP server.

        Connects to the server if not already warm.

        :param agent_id: The owning agent's id, e.g. ``"agent_abc123"``.
        :param spec: The agent's spec.
        :param server_name: The MCP server to call, e.g. ``"github"``.
        :param tool_name: The bare (un-namespaced) tool name,
            e.g. ``"search"`` (not ``"github__search"``).
        :param arguments: The tool arguments dict, already parsed from
            JSON, e.g. ``{"query": "python asyncio"}``.
        :returns: Tool result as a string (same shape as
            :meth:`omnigent.tools.mcp.McpServerConnection.call_tool`).
        :raises RuntimeError: If the agent has no MCP servers, the named
            server is not found, or the server has no live connection.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            raise RuntimeError(f"agent {agent_id!r} has no MCP servers configured")
        await self._ensure_warm(agent_id, configs, _agent_credential_broker(spec))

        async with self._lock:
            entry = self._entries.get(agent_id)
        if entry is None:
            raise RuntimeError(f"failed to initialize MCP pool for agent {agent_id!r}")

        server = entry.servers.get(server_name)
        if server is None:
            raise RuntimeError(f"agent {agent_id!r} has no MCP server named {server_name!r}")
        if server.error is not None:
            raise RuntimeError(
                f"MCP server {server_name!r} (agent {agent_id!r}) is unhealthy: {server.error}"
            )
        if server.connection is None:
            raise RuntimeError(
                f"MCP server {server_name!r} (agent {agent_id!r}) has no live "
                "connection — connect() may have been skipped"
            )
        return await server.connection.call_tool(tool_name, arguments)

    async def shutdown_for(self, agent_id: str) -> None:
        """Close all connections for an agent and remove its pool entry.

        Safe to call when the agent has no pool entry (no-op). Intended
        for use on agent deletion or session end.

        :param agent_id: The agent to shut down, e.g. ``"agent_abc123"``.
        """
        async with self._lock:
            entry = self._entries.pop(agent_id, None)
            with contextlib.suppress(ValueError):
                self._lru.remove(agent_id)
        if entry is not None:
            await self._close_entry(entry)

    async def shutdown_all(self) -> None:
        """Close all connections and clear the pool.

        Called on AP-server shutdown. Best-effort — individual close
        failures are logged and swallowed.
        """
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._lru.clear()
        for entry in entries:
            await self._close_entry(entry)

    async def _ensure_warm(
        self,
        agent_id: str,
        configs: list[MCPServerConfig],
        credential_broker: CredentialBrokerSpec | None = None,
    ) -> None:
        """Ensure connections are warm for *agent_id*; await the prewarm task.

        Creates a new entry if none exists for *agent_id*, or replaces
        a stale entry when the spec hash has changed. The prewarm task
        is awaited **outside** the lock so concurrent requests can
        proceed in parallel.

        :param agent_id: The agent whose pool entry to warm,
            e.g. ``"agent_abc123"``.
        :param configs: The agent's MCP server configs (pre-extracted
            from ``spec.mcp_servers``).
        """
        spec_hash = compute_spec_hash(configs, credential_broker=credential_broker)
        prewarm: asyncio.Task[None] | None = None

        async with self._lock:
            entry = self._entries.get(agent_id)

            # Spec changed → evict old connections and start fresh.
            if entry is not None and entry.spec_hash != spec_hash:
                old = self._entries.pop(agent_id, None)
                with contextlib.suppress(ValueError):
                    self._lru.remove(agent_id)
                if old is not None:
                    t = asyncio.create_task(
                        self._close_entry(old),
                        name=f"server-mcp-evict:{agent_id}",
                    )
                    self._evict_tasks.add(t)
                    t.add_done_callback(self._evict_tasks.discard)
                entry = None

            if entry is None:
                self._enforce_capacity()
                entry = _AgentEntry(
                    agent_id=agent_id,
                    spec_hash=spec_hash,
                    servers={cfg.name: _McpServerEntry(config=cfg) for cfg in configs},
                    credential_broker=credential_broker,
                )
                self._entries[agent_id] = entry
                self._lru.append(agent_id)
            else:
                self._touch(agent_id)

            needs_connect = any(s.connection is None for s in entry.servers.values())
            if needs_connect and (entry.prewarm_task is None or entry.prewarm_task.done()):
                entry.prewarm_task = asyncio.create_task(
                    self._connect_all(entry),
                    name=f"server-mcp-prewarm:{agent_id}",
                )
            prewarm = entry.prewarm_task

        if prewarm is not None:
            try:
                await prewarm
            except Exception:
                _logger.exception("server MCP prewarm raised for agent %r", agent_id)

    async def _connect_all(self, entry: _AgentEntry) -> None:
        """Connect all not-yet-connected servers in *entry* concurrently.

        :param entry: The agent's pool entry to populate.
        """
        tasks = [
            asyncio.create_task(
                self._connect_server(server, entry.credential_broker),
                name=f"server-mcp-connect:{entry.agent_id}:{server.config.name}",
            )
            for server in entry.servers.values()
            if server.connection is None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _connect_server(
        self,
        server: _McpServerEntry,
        credential_broker: CredentialBrokerSpec | None = None,
    ) -> None:
        """Connect one MCP server, storing tools on success or error on failure.

        :param server: The server entry to connect. Modified in place:
            sets ``connection``, ``tools``, and ``error``.
        :param credential_broker: Non-HTTP broker spec for resolving the
            server's ``credential_groups`` into its stdio spawn env.
        """
        try:
            conn = McpServerConnection(config=server.config, credential_broker=credential_broker)
            tools = await conn.connect()
            server.connection = conn
            server.tools = list(tools)
            server.error = None
        except Exception as exc:
            server.error = f"{type(exc).__name__}: {exc}"
            _logger.exception(
                "MCP server %r failed to connect in ServerMcpPool",
                server.config.name,
            )

    async def _close_entry(self, entry: _AgentEntry) -> None:
        """Best-effort close of all connections in *entry*.

        :param entry: The agent entry to tear down.
        """
        if entry.prewarm_task is not None and not entry.prewarm_task.done():
            entry.prewarm_task.cancel()
        for server in entry.servers.values():
            if server.connection is not None:
                try:
                    await server.connection.close()
                except Exception:
                    _logger.exception(
                        "error closing MCP %r during ServerMcpPool shutdown",
                        server.config.name,
                    )

    def _touch(self, agent_id: str) -> None:
        """Move *agent_id* to the most-recently-used end of the LRU list.

        :param agent_id: The agent to promote, e.g. ``"agent_abc123"``.
        """
        with contextlib.suppress(ValueError):
            self._lru.remove(agent_id)
        self._lru.append(agent_id)

    def _enforce_capacity(self) -> None:
        """Evict the least-recently-used agent entry if at capacity.

        Called while holding ``_lock``. Fires close tasks in the
        background so the caller is never blocked by connection teardown.
        """
        while len(self._entries) >= _POOL_AGENT_CAPACITY:
            oldest = self._lru.pop(0)
            old_entry = self._entries.pop(oldest, None)
            if old_entry is not None:
                t = asyncio.create_task(
                    self._close_entry(old_entry),
                    name=f"server-mcp-evict:{oldest}",
                )
                self._evict_tasks.add(t)
                t.add_done_callback(self._evict_tasks.discard)
