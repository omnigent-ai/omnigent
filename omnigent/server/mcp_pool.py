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
from typing import Any, Literal

from mcp.types import Tool as McpToolDef

from omnigent.runner.mcp_manager import (  # noqa: F401 — McpSchemasResult re-exported for callers
    McpSchemasResult,
    compute_spec_hash,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.tools.base import is_valid_tool_name
from omnigent.tools.mcp import (
    _DEFAULT_AUTH_COMMAND_TIMEOUT_S,
    CredentialResolver,
    McpServerConnection,
)

_logger = logging.getLogger(__name__)

# Maximum number of live MCP server connections the pool retains.
# When capacity is reached the least-recently-used entry is closed
# and evicted. _POOL_AGENT_CAPACITY is kept as a legacy alias.
_POOL_CONNECTION_CAPACITY = 32
_POOL_AGENT_CAPACITY = _POOL_CONNECTION_CAPACITY

_STATIC_AUTH_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }
)

_PoolKey = tuple[str, str | None]


def _pool_capacity() -> int:
    """Return the configured connection capacity, honoring legacy alias overrides."""
    if _POOL_CONNECTION_CAPACITY != 32:
        return _POOL_CONNECTION_CAPACITY
    return _POOL_AGENT_CAPACITY


def _normalize_user_id(user_id: str | None) -> str | None:
    """Normalize empty/whitespace user IDs to ``None``."""
    if user_id is None:
        return None
    stripped = user_id.strip()
    return stripped or None


def _static_credential_servers(configs: list[MCPServerConfig]) -> list[str]:
    """Return names of HTTP servers carrying static credentials without ``auth_command``."""
    flagged: list[str] = []
    for cfg in configs:
        if cfg.transport != "http" or cfg.auth_command is not None:
            continue
        has_static_header = any(
            header_name.lower() in _STATIC_AUTH_HEADER_NAMES for header_name in (cfg.headers or {})
        )
        if has_static_header or cfg.databricks_profile is not None:
            flagged.append(cfg.name)
    return flagged


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
class _PoolEntry:
    """Pool entry keyed by ``(agent_id, user_id)``.

    Connections are isolated per calling user so credentials resolved for
    one user are never reused by another. The entry is invalidated when
    the spec hash changes (agent re-deployed with different MCP servers).

    :param agent_id: The agent this entry belongs to,
        e.g. ``"agent_abc123"``.
    :param user_id: Normalized calling user identifier, or ``None`` when
        unscoped.
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
    user_id: str | None = None
    servers: dict[str, _McpServerEntry] = field(default_factory=dict)
    prewarm_task: asyncio.Task[None] | None = None


_AgentEntry = _PoolEntry


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

    Manages MCP connections isolated per ``(agent_id, user_id)``. An entry
    is populated lazily on first use (warm-on-demand). Connections stay
    live for the lifetime of the entry. An LRU eviction policy caps total
    live connections at :data:`_POOL_CONNECTION_CAPACITY`.

    Tool namespacing (``server_name__tool_name``) is **not** applied
    internally — it is the responsibility of callers to namespace names
    in ``tools/list`` responses and to de-namespace before calling
    :meth:`call_tool`.

    Usage::

        pool = ServerMcpPool()

        # List tools for an agent and user:
        entries = await pool.list_tools(agent_id, spec, user_id="user_1")
        namespaced = [f"{e.server_name}__{e.tool.name}" for e in entries]

        # Call a tool (de-namespace first):
        result = await pool.call_tool(agent_id, spec, "github", "search", args, user_id="user_1")

        # Tear down on agent or user deletion:
        await pool.shutdown_for(agent_id)

        # Tear down everything on server shutdown:
        await pool.shutdown_all()
    """

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver | None = None,
        auth_command_timeout_s: float = _DEFAULT_AUTH_COMMAND_TIMEOUT_S,
        require_user_id: bool = False,
        shared_credential_policy: Literal["warn", "refuse"] = "warn",
    ) -> None:
        """Initialize an empty pool.

        :param credential_resolver: Optional callback ``(config, user_id)``
            returning HTTP headers for the calling user (sync or async).
        :param auth_command_timeout_s: Timeout in seconds for per-server
            ``auth_command`` invocations.
        :param require_user_id: When ``True``, reject ``list_tools`` and
            ``call_tool`` calls without a non-empty ``user_id``.
        :param shared_credential_policy: Controls what happens when more
            than one distinct user accesses the same agent whose HTTP MCP
            servers carry static credentials without per-user resolution
            (``"warn"`` logs a warning; ``"refuse"`` raises ``RuntimeError``).
        """
        if shared_credential_policy not in ("warn", "refuse"):
            raise ValueError(
                f"invalid shared_credential_policy {shared_credential_policy!r}; "
                "must be 'warn' or 'refuse'"
            )
        self._credential_resolver = credential_resolver
        self._auth_command_timeout_s = auth_command_timeout_s
        self._require_user_id = require_user_id
        self._shared_credential_policy = shared_credential_policy
        self._entries: dict[_PoolKey, _PoolEntry] = {}
        # Most-recent (agent_id, user_id) at end; front is the eviction candidate.
        self._lru: list[_PoolKey] = []
        self._seen_users_by_agent: dict[str, set[str | None]] = {}
        self._lock = asyncio.Lock()
        # Hold strong references to fire-and-forget eviction tasks so
        # the GC does not cancel them mid-flight (RUF006).
        self._evict_tasks: set[asyncio.Task[None]] = set()

    def _resolve_pool_key(self, agent_id: str, user_id: str | None) -> _PoolKey:
        """Normalize *user_id* and enforce ``require_user_id`` when enabled."""
        normalized_user = _normalize_user_id(user_id)
        if self._require_user_id and normalized_user is None:
            raise ValueError(f"user_id is required for MCP pool operations on agent {agent_id!r}")
        return (agent_id, normalized_user)

    def _check_shared_static_credentials(
        self,
        agent_id: str,
        user_key: str | None,
        configs: list[MCPServerConfig],
    ) -> None:
        """Warn or refuse if multiple users share static MCP credentials on *agent_id*."""
        seen = self._seen_users_by_agent.setdefault(agent_id, set())
        if user_key in seen or not seen:
            seen.add(user_key)
            return
        if self._credential_resolver is not None:
            seen.add(user_key)
            return
        static_servers = _static_credential_servers(configs)
        if not static_servers:
            seen.add(user_key)
            return
        servers_desc = ", ".join(static_servers)
        if self._shared_credential_policy == "refuse":
            raise RuntimeError(
                f"Refusing to share static MCP credentials across multiple users "
                f"for agent {agent_id!r}: servers ({servers_desc}) have static "
                "credentials without per-user auth_command or credential_resolver"
            )
        _logger.warning(
            "Agent %r shares static MCP credentials across multiple users "
            "(servers: %s) without per-user auth_command or credential_resolver",
            agent_id,
            servers_desc,
        )
        seen.add(user_key)

    async def list_tools(
        self,
        agent_id: str,
        spec: AgentSpec,
        user_id: str | None = None,
    ) -> list[McpToolEntry]:
        """Return MCP tool definitions for all of the agent's servers.

        Connects to servers if not already warm. Applies the per-server
        ``tools`` allow-list from :class:`MCPServerConfig` when present.

        :param agent_id: The agent whose tools to list,
            e.g. ``"agent_abc123"``.
        :param spec: The agent's spec; reads ``spec.mcp_servers``.
        :param user_id: Optional calling user identifier used to scope
            pool entries and resolve per-user credentials.
        :returns: A list of :class:`McpToolEntry` objects, one per
            allowed tool per server. Empty when the spec has no
            ``mcp_servers``.
        """
        pool_key = self._resolve_pool_key(agent_id, user_id)
        configs = list(spec.mcp_servers or [])
        if not configs:
            return []
        await self._ensure_warm(pool_key, configs)

        async with self._lock:
            entry = self._entries.get(pool_key)
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
        user_id: str | None = None,
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
        :param user_id: Optional calling user identifier used to scope
            pool entries and resolve per-user credentials.
        :returns: Tool result as a string (same shape as
            :meth:`omnigent.tools.mcp.McpServerConnection.call_tool`).
        :raises RuntimeError: If the agent has no MCP servers, the named
            server is not found, or the server has no live connection.
        """
        pool_key = self._resolve_pool_key(agent_id, user_id)
        configs = list(spec.mcp_servers or [])
        if not configs:
            raise RuntimeError(f"agent {agent_id!r} has no MCP servers configured")
        await self._ensure_warm(pool_key, configs)

        async with self._lock:
            entry = self._entries.get(pool_key)
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

    async def shutdown_for(self, agent_id: str, user_id: str | None = None) -> None:
        """Close connections for *agent_id* (optionally scoped to *user_id*).

        When ``user_id`` is ``None``, closes and removes every user's
        entry for ``agent_id``. When ``user_id`` is given, closes only
        that user's entry. Safe to call when no matching entry exists.

        :param agent_id: The agent to shut down, e.g. ``"agent_abc123"``.
        :param user_id: Optional calling user identifier; ``None`` shuts
            down all user entries for *agent_id*.
        """
        normalized_user = _normalize_user_id(user_id)
        to_close: list[_PoolEntry] = []
        async with self._lock:
            if normalized_user is None:
                keys = [k for k in self._entries if k[0] == agent_id]
                for key in keys:
                    entry = self._entries.pop(key, None)
                    with contextlib.suppress(ValueError):
                        self._lru.remove(key)
                    if entry is not None:
                        to_close.append(entry)
                self._seen_users_by_agent.pop(agent_id, None)
            else:
                key = (agent_id, normalized_user)
                entry = self._entries.pop(key, None)
                with contextlib.suppress(ValueError):
                    self._lru.remove(key)
                if entry is not None:
                    to_close.append(entry)
        for entry in to_close:
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
            self._seen_users_by_agent.clear()
        for entry in entries:
            await self._close_entry(entry)

    async def _ensure_warm(
        self,
        pool_key: _PoolKey,
        configs: list[MCPServerConfig],
    ) -> None:
        """Ensure connections are warm for *pool_key*; await the prewarm task.

        Creates a new entry if none exists for *pool_key*, or replaces a
        stale entry when the spec hash has changed. The prewarm task is
        awaited **outside** the lock so concurrent requests can proceed
        in parallel.

        :param pool_key: ``(agent_id, user_id)`` key to warm.
        :param configs: The agent's MCP server configs (pre-extracted
            from ``spec.mcp_servers``).
        """
        agent_id, user_id = pool_key
        needed_connections = len(configs)
        capacity = _pool_capacity()
        if needed_connections > capacity:
            raise ValueError(
                f"agent {agent_id!r} configures {needed_connections} MCP servers, "
                f"exceeding pool connection capacity ({capacity})"
            )
        spec_hash = compute_spec_hash(configs, user_id=user_id)
        prewarm: asyncio.Task[None] | None = None

        async with self._lock:
            self._check_shared_static_credentials(agent_id, user_id, configs)
            entry = self._entries.get(pool_key)

            # Spec changed → evict old connections and start fresh.
            if entry is not None and entry.spec_hash != spec_hash:
                old = self._entries.pop(pool_key, None)
                with contextlib.suppress(ValueError):
                    self._lru.remove(pool_key)
                if old is not None:
                    t = asyncio.create_task(
                        self._close_entry(old),
                        name=f"server-mcp-evict:{agent_id}:{user_id or 'anon'}",
                    )
                    self._evict_tasks.add(t)
                    t.add_done_callback(self._evict_tasks.discard)
                entry = None

            if entry is None:
                self._enforce_capacity(needed_connections, user_id=user_id)
                entry = _PoolEntry(
                    agent_id=agent_id,
                    user_id=user_id,
                    spec_hash=spec_hash,
                    servers={cfg.name: _McpServerEntry(config=cfg) for cfg in configs},
                )
                self._entries[pool_key] = entry
                self._lru.append(pool_key)
            else:
                self._touch(pool_key)

            needs_connect = any(s.connection is None for s in entry.servers.values())
            if needs_connect and (entry.prewarm_task is None or entry.prewarm_task.done()):
                entry.prewarm_task = asyncio.create_task(
                    self._connect_all(entry),
                    name=f"server-mcp-prewarm:{agent_id}:{user_id or 'anon'}",
                )
            prewarm = entry.prewarm_task

        if prewarm is not None:
            try:
                await prewarm
            except Exception:
                _logger.exception(
                    "server MCP prewarm raised for agent %r (user %r)",
                    agent_id,
                    user_id,
                )

    async def _connect_all(self, entry: _PoolEntry) -> None:
        """Connect all not-yet-connected servers in *entry* concurrently.

        :param entry: The pool entry to populate.
        """
        user_label = entry.user_id or "anon"
        tasks = [
            asyncio.create_task(
                self._connect_server(server, user_id=entry.user_id),
                name=f"server-mcp-connect:{entry.agent_id}:{user_label}:{server.config.name}",
            )
            for server in entry.servers.values()
            if server.connection is None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _connect_server(
        self,
        server: _McpServerEntry,
        *,
        user_id: str | None = None,
    ) -> None:
        """Connect one MCP server, storing tools on success or error on failure.

        :param server: The server entry to connect. Modified in place:
            sets ``connection``, ``tools``, and ``error``.
        :param user_id: Optional calling user identifier for credential
            resolution.
        """
        try:
            conn_kwargs: dict[str, Any] = {}
            if user_id is not None:
                conn_kwargs["user_id"] = user_id
            if self._credential_resolver is not None:
                conn_kwargs["credential_resolver"] = self._credential_resolver
            if self._auth_command_timeout_s != _DEFAULT_AUTH_COMMAND_TIMEOUT_S:
                conn_kwargs["auth_command_timeout_s"] = self._auth_command_timeout_s
            conn = McpServerConnection(config=server.config, **conn_kwargs)
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

    async def _close_entry(self, entry: _PoolEntry) -> None:
        """Best-effort close of all connections in *entry*.

        :param entry: The pool entry to tear down.
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

    def _touch(self, pool_key: _PoolKey) -> None:
        """Move *pool_key* to the most-recently-used end of the LRU list.

        :param pool_key: The ``(agent_id, user_id)`` key to promote.
        """
        with contextlib.suppress(ValueError):
            self._lru.remove(pool_key)
        self._lru.append(pool_key)

    def _live_connection_count(self) -> int:
        """Return the total number of server slots held across active pool entries."""
        return sum(len(entry.servers) for entry in self._entries.values())

    def _user_connection_count(self, user_id: str | None) -> int:
        """Return the number of server slots held by *user_id* across active entries."""
        return sum(len(entry.servers) for key, entry in self._entries.items() if key[1] == user_id)

    def _enforce_capacity(
        self,
        needed_connections: int = 1,
        *,
        user_id: str | None = None,
    ) -> None:
        """Evict least-recently-used entries until *needed_connections* fit in capacity.

        When a single *user_id* already holds entries and would exceed half the
        pool while other users also hold entries, evicts *user_id*'s oldest
        entry first so one user cannot evict everyone else's connections.
        Called while holding ``_lock``. Fires close tasks in the
        background so the caller is never blocked by connection teardown.
        """
        capacity = _pool_capacity()
        per_user_cap = max(1, capacity // 2)
        while self._lru and self._live_connection_count() + needed_connections > capacity:
            victim_index = 0
            if (
                user_id is not None
                and self._user_connection_count(user_id) + needed_connections > per_user_cap
                and any(k[1] == user_id for k in self._lru)
                and any(k[1] != user_id for k in self._lru)
            ):
                victim_index = next(i for i, k in enumerate(self._lru) if k[1] == user_id)
            oldest = self._lru.pop(victim_index)
            old_entry = self._entries.pop(oldest, None)
            if old_entry is not None:
                t = asyncio.create_task(
                    self._close_entry(old_entry),
                    name=f"server-mcp-evict:{oldest[0]}:{oldest[1] or 'anon'}",
                )
                self._evict_tasks.add(t)
                t.add_done_callback(self._evict_tasks.discard)
