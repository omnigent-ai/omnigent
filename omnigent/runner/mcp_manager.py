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

from omnigent.inner.datamodel import CredentialBrokerSpec
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
class _ServerEntry:
    """One MCP server within a spec's pool entry."""

    config: MCPServerConfig
    connection: McpServerConnection | None = None
    tools: list[McpToolDef] = field(default_factory=list)
    error: str | None = None


@dataclass
class _SpecEntry:
    spec_hash: str
    servers: dict[str, _ServerEntry] = field(default_factory=dict)
    prewarm_task: asyncio.Task[None] | None = None
    credential_broker: CredentialBrokerSpec | None = None


@dataclass(frozen=True)
class McpSchemasResult:
    """Output of :meth:`RunnerMcpManager.schemas_for`."""

    schemas: list[dict[str, Any]]
    tool_names: set[str]
    failures: dict[str, str]  # server_name → error message


def compute_spec_hash(
    configs: list[MCPServerConfig],
    cwd: Path | None = None,
    credential_broker: CredentialBrokerSpec | None = None,
) -> str:
    """Stable content hash over ``spec.mcp_servers`` (+ stdio cwd + broker).

    The credential broker is folded in so a broker change (rotated load
    source, new fallback) invalidates cached connections even when the
    ``mcp_servers`` list is unchanged. ``repr`` is a stable serialization of
    the (secret-value-free) broker spec.
    """
    payload = json.dumps(
        {
            "cwd": str(cwd) if cwd is not None else None,
            "credential_broker": (
                repr(credential_broker) if credential_broker is not None else None
            ),
            "servers": [
                {
                    "name": c.name,
                    "transport": c.transport,
                    "url": c.url,
                    "command": c.command,
                    "args": list(c.args or []),
                    "env": dict(c.env or {}),
                    "tools": list(getattr(c, "tools", None) or []),
                    "credential_groups": list(getattr(c, "credential_groups", None) or []),
                }
                for c in configs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _agent_credential_broker(spec: AgentSpec) -> CredentialBrokerSpec | None:
    """Extract the non-HTTP credential broker spec from an agent's os_env.

    Returns ``None`` when the agent has no ``os_env``, no ``sandbox``, or no
    ``credential_broker`` configured.
    """
    os_env = getattr(spec, "os_env", None)
    sandbox = getattr(os_env, "sandbox", None)
    return getattr(sandbox, "credential_broker", None)


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
        """Fire-and-forget background spawn of *spec*'s MCPs. Idempotent."""
        configs = list(spec.mcp_servers or [])
        if not configs:
            return
        broker = _agent_credential_broker(spec)
        spec_hash = compute_spec_hash(configs, self._stdio_cwd, broker)
        async with self._lock:
            entry = self._ensure_entry(spec_hash, configs, broker)
            if entry.prewarm_task is None or entry.prewarm_task.done():
                entry.prewarm_task = asyncio.create_task(
                    self._connect_all(entry),
                    name=f"runner-mcp-prewarm:{spec_hash}",
                )

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        """Resolve MCP schemas for *spec*; awaits any in-flight prewarm."""
        configs = list(spec.mcp_servers or [])
        if not configs:
            return McpSchemasResult(schemas=[], tool_names=set(), failures={})
        broker = _agent_credential_broker(spec)
        spec_hash = compute_spec_hash(configs, self._stdio_cwd, broker)
        async with self._lock:
            entry = self._ensure_entry(spec_hash, configs, broker)
            self._touch(spec_hash)
            prewarm = entry.prewarm_task
            needs_connect = any(s.connection is None for s in entry.servers.values())
            if needs_connect and (prewarm is None or prewarm.done()):
                entry.prewarm_task = asyncio.create_task(
                    self._connect_all(entry),
                    name=f"runner-mcp-on-demand:{spec_hash}",
                )
                prewarm = entry.prewarm_task

        # Await outside the lock so concurrent prewarms can proceed.
        if prewarm is not None:
            try:
                await prewarm
            except Exception:
                _logger.exception("runner mcp prewarm task raised; surfacing partial results")

        schemas: list[dict[str, Any]] = []
        tool_names: set[str] = set()
        failures: dict[str, str] = {}
        for server in entry.servers.values():
            if server.error is not None:
                failures[server.config.name] = server.error
                continue
            allowed = (
                set(getattr(server.config, "tools", None) or [])
                if getattr(server.config, "tools", None)
                else None
            )
            for td in server.tools:
                schema = _mcp_tool_schema(server.config.name, td, allowed)
                if schema is None:
                    continue
                schemas.append(schema)
                tool_names.add(schema["name"])
        return McpSchemasResult(schemas=schemas, tool_names=tool_names, failures=failures)

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
        spec_hash = compute_spec_hash(configs, self._stdio_cwd, _agent_credential_broker(spec))
        entry = self._specs.get(spec_hash)
        if entry is None:
            # Dispatch before schemas_for(): populate + await prewarm.
            await self.schemas_for(spec)
            entry = self._specs.get(spec_hash)
        if entry is None:
            raise RuntimeError(f"runner failed to initialize MCPs for spec {spec.name!r}")

        # Tool names from the LLM are namespaced as ``{server}__{bare}``
        # (see ``_mcp_tool_schema``).  Strip the prefix to get the bare
        # name used by the MCP server internally.
        bare_name = tool_name
        owning_server: _ServerEntry | None = None
        for server in entry.servers.values():
            if server.error is not None:
                continue
            prefix = f"{server.config.name}__"
            if tool_name.startswith(prefix):
                candidate = tool_name[len(prefix) :]
                if any(td.name == candidate for td in server.tools):
                    bare_name = candidate
                    owning_server = server
                    break
            elif any(td.name == tool_name for td in server.tools):
                # Caller already passed the bare name (e.g. in tests).
                owning_server = server
                break
        if owning_server is None or owning_server.connection is None:
            raise RuntimeError(f"runner has no live MCP serving tool {tool_name!r}")

        return await owning_server.connection.call_tool(
            bare_name,
            arguments,
            session_id=session_id,
        )

    def _resolve_owning_server(
        self,
        spec: AgentSpec,
        bare_tool: str,
    ) -> _ServerEntry | None:
        """
        Find the server entry that owns *bare_tool*.

        Used by the MRTR retry path in ``/mcp/execute`` to access
        the ``McpServerConnection`` directly for
        ``call_tool_with_elicitation``.

        :param spec: Agent spec whose MCP servers to search.
        :param bare_tool: The bare tool name (no namespace prefix),
            e.g. ``"get_me"``.
        :returns: The owning ``_ServerEntry``, or ``None`` if the
            tool is not found.
        """
        configs = list(spec.mcp_servers or [])
        if not configs:
            return None
        spec_hash = compute_spec_hash(configs, self._stdio_cwd, _agent_credential_broker(spec))
        entry = self._specs.get(spec_hash)
        if entry is None:
            return None
        for server in entry.servers.values():
            if server.error is not None:
                continue
            if any(td.name == bare_tool for td in server.tools):
                return server
        return None

    async def shutdown(self) -> None:
        """Best-effort close of every active MCP connection."""
        for spec_hash, entry in list(self._specs.items()):
            if entry.prewarm_task is not None and not entry.prewarm_task.done():
                entry.prewarm_task.cancel()
            for server in entry.servers.values():
                if server.connection is None:
                    continue
                try:
                    await server.connection.close()
                except Exception:
                    _logger.exception(
                        "error closing MCP %r in spec %s during shutdown",
                        server.config.name,
                        spec_hash,
                    )
        self._specs.clear()
        self._lru.clear()

    def _ensure_entry(
        self,
        spec_hash: str,
        configs: list[MCPServerConfig],
        credential_broker: CredentialBrokerSpec | None = None,
    ) -> _SpecEntry:
        """Return or create the pool entry for *spec_hash*. Caller holds lock."""
        entry = self._specs.get(spec_hash)
        if entry is not None:
            return entry
        entry = _SpecEntry(spec_hash=spec_hash, credential_broker=credential_broker)
        for cfg in configs:
            entry.servers[cfg.name] = _ServerEntry(config=cfg)
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
            if entry.prewarm_task is not None and not entry.prewarm_task.done():
                entry.prewarm_task.cancel()
            for server in entry.servers.values():
                if server.connection is not None:
                    task = asyncio.create_task(
                        self._safe_close(server.connection, victim, server.config.name),
                        name=f"runner-mcp-evict-close:{victim}:{server.config.name}",
                    )
                    self._evict_tasks.add(task)
                    task.add_done_callback(self._evict_tasks.discard)

    @staticmethod
    async def _safe_close(conn: McpServerConnection, spec_hash: str, name: str) -> None:
        try:
            await conn.close()
        except Exception:
            _logger.exception("error closing evicted MCP %r in spec %s", name, spec_hash)

    async def _connect_all(self, entry: _SpecEntry) -> None:
        """Connect every MCP in *entry* concurrently. Failures recorded per server."""

        async def _one(server: _ServerEntry) -> None:
            if server.connection is not None:
                return
            try:
                conn = McpServerConnection(
                    config=server.config,
                    cwd=self._stdio_cwd,
                    elicitation_callback=self._build_elicitation_callback(),
                    credential_broker=entry.credential_broker,
                )
                tools = await conn.connect()
                server.connection = conn
                server.tools = tools
                server.error = None
                _logger.info(
                    "runner mcp connected: spec=%s server=%s tools=%d",
                    entry.spec_hash,
                    server.config.name,
                    len(tools),
                )
            except Exception as exc:  # noqa: BLE001
                server.error = f"{type(exc).__name__}: {exc}"
                server.connection = None
                server.tools = []
                _logger.warning(
                    "runner mcp connect failed: spec=%s server=%s error=%s",
                    entry.spec_hash,
                    server.config.name,
                    server.error,
                )

        await asyncio.gather(*[_one(s) for s in entry.servers.values()])

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
                            if s.connection is not None and s.error is None
                            else ("failed" if s.error else "pending"),
                            "tools": [t.name for t in s.tools],
                            "error": s.error,
                        }
                        for s in entry.servers.values()
                    ],
                }
            )
        return {"specs": out_specs}
