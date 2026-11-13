"""Unit tests for :class:`ServerMcpPool`.

Covers the non-trivial logic in the AP-server-side MCP connection pool:

- ``list_tools`` with empty spec → empty list without connecting
- ``list_tools`` happy path: tools returned per server
- ``list_tools`` with tools allow-list → only allowed tools surfaced
- ``list_tools`` with invalid tool name → tool silently skipped
- ``list_tools`` when a server fails to connect → server skipped, others visible
- ``call_tool`` happy path
- ``call_tool`` with no MCP servers → RuntimeError
- ``call_tool`` with unknown server name → RuntimeError
- ``call_tool`` when the server is unhealthy → RuntimeError
- ``shutdown_for`` closes connections and removes pool entry
- ``shutdown_all`` closes all entries and empties the pool
- spec hash invalidation: changed spec evicts the old entry and rebuilds
- LRU eviction at capacity: least-recently-used agent evicted on overflow
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from mcp.types import Tool as McpToolDef

from omnigent.server import mcp_pool as _mcp_pool_module
from omnigent.server.mcp_pool import McpToolEntry, ServerMcpPool
from omnigent.spec.types import AgentSpec, MCPServerConfig

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_config(name: str, tools: list[str] | None = None) -> MCPServerConfig:
    """HTTP MCPServerConfig with an optional tools allow-list.

    ``MCPServerConfig`` has no ``tools`` field in its constructor — the allow-list
    is a dynamic attribute read by :meth:`ServerMcpPool.list_tools` via
    ``getattr(config, "tools", None)``.  We set it directly on the instance
    when the test needs to exercise the filter path.

    :param name: The server name, e.g. ``"github"``.
    :param tools: Optional list of tool names for the allow-list.
    :returns: An :class:`MCPServerConfig` pointing at a dummy URL.
    """
    config = MCPServerConfig(
        name=name,
        transport="http",
        url=f"http://mcp/{name}",
    )
    if tools is not None:
        config.tools = tools  # type: ignore[attr-defined]  — dynamic allow-list attr
    return config


def _make_spec(*configs: MCPServerConfig) -> AgentSpec:
    """AgentSpec with the given MCPServerConfigs and nothing else.

    :param configs: The MCP server configs to include.
    :returns: A minimal :class:`AgentSpec` with ``mcp_servers`` populated.
    """
    return AgentSpec(spec_version=1, name="test-agent", mcp_servers=list(configs))


def _empty_spec() -> AgentSpec:
    """AgentSpec with no MCP servers.

    :returns: A minimal :class:`AgentSpec` with an empty ``mcp_servers`` list.
    """
    return AgentSpec(spec_version=1, name="test-agent")


def _make_tool(name: str, description: str = "test tool") -> McpToolDef:
    """Minimal MCP tool definition.

    :param name: Tool name, e.g. ``"search"``.
    :param description: Human-readable description.
    :returns: A :class:`McpToolDef` with an empty inputSchema.
    """
    return McpToolDef(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
    )


@dataclass
class _FakeConn:
    """Stand-in for McpServerConnection; records connect / close / call_tool.

    :param tools: The tool list to return from ``connect()``.
    :param connect_error: If set, ``connect()`` raises this exception.
    """

    tools: list[McpToolDef]
    connect_error: Exception | None = None
    connect_calls: int = 0
    close_calls: int = 0
    call_tool_results: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Initialize mutable defaults."""
        if self.call_tool_results is None:
            self.call_tool_results = {}

    async def connect(self) -> list[McpToolDef]:
        """Simulate connect; either raise or return tools.

        :returns: The canned tool list.
        :raises: ``connect_error`` if set.
        """
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        return self.tools

    async def close(self) -> None:
        """Record close."""
        self.close_calls += 1

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Return a scripted result for *name*, or a default stub.

        :param name: Tool name.
        :param arguments: Tool arguments (unused).
        :returns: Scripted result or generic stub string.
        """
        return self.call_tool_results.get(name, f"result:{name}")


@pytest.fixture()
def patch_mcp_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, _FakeConn]:
    """Patch ``McpServerConnection`` in the mcp_pool module with _FakeConn stubs.

    Returns a dict the test can pre-populate with per-server _FakeConn objects
    **before** calling pool methods. If a name is missing from the dict a
    default (no tools, no error) _FakeConn is inserted on first access.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: Mutable dict ``{server_name: _FakeConn}`` for test scripting.
    """
    conns: dict[str, _FakeConn] = {}

    class _FakeMcpServerConnection:
        """Drop-in for McpServerConnection; delegates to the closure dict."""

        def __init__(
            self,
            *,
            config: MCPServerConfig,
            user_id: str | None = None,
            credential_resolver: Any = None,
            auth_command_timeout_s: float | None = None,
        ) -> None:
            """Register a stub for *config.name* (or ``name:user_id``) if absent."""
            self.config = config
            self.user_id = user_id
            self.credential_resolver = credential_resolver
            self.auth_command_timeout_s = auth_command_timeout_s
            user_key = f"{config.name}:{user_id}" if user_id is not None else config.name
            if user_id is not None and user_key in conns:
                self._key = user_key
            else:
                self._key = config.name
                if self._key not in conns:
                    conns[self._key] = _FakeConn(tools=[])

        async def connect(self) -> list[McpToolDef]:
            """Forward to the per-name _FakeConn."""
            return await conns[self._key].connect()

        async def close(self) -> None:
            """Forward close to the per-name _FakeConn."""
            await conns[self._key].close()

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
            """Forward call_tool to the per-name _FakeConn."""
            return await conns[self._key].call_tool(name, arguments)

    monkeypatch.setattr(
        "omnigent.server.mcp_pool.McpServerConnection",
        _FakeMcpServerConnection,
    )
    return conns


# ── list_tools ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tools_empty_spec_returns_empty() -> None:
    """``list_tools`` must return an empty list when spec has no MCP servers.

    Failure means the pool attempts to connect when there is nothing to connect
    to, wasting resources for agents without MCP tools.
    """
    pool = ServerMcpPool()
    result = await pool.list_tools("agent_1", _empty_spec())
    assert result == [], "Empty spec must yield empty list without any pool activity"
    await pool.shutdown_all()


@pytest.mark.asyncio
async def test_list_tools_happy_path(patch_mcp_connection: dict[str, _FakeConn]) -> None:
    """``list_tools`` must return McpToolEntry objects for each tool on each server.

    Failure means tools are not surfaced to the session endpoint and the agent
    has no MCP tools available despite a healthy connection.
    """
    patch_mcp_connection["github"] = _FakeConn(
        tools=[_make_tool("search"), _make_tool("create_issue")]
    )
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    try:
        entries = await pool.list_tools("agent_1", spec)
    finally:
        await pool.shutdown_all()

    tool_names = {e.tool.name for e in entries}
    assert tool_names == {"search", "create_issue"}, (
        "Both tools from the server must appear in the result"
    )
    # Verify entries carry the correct server_name for namespacing by the caller
    for entry in entries:
        assert isinstance(entry, McpToolEntry)
        assert entry.server_name == "github", "server_name must match the MCPServerConfig name"


@pytest.mark.asyncio
async def test_list_tools_apply_allow_list(patch_mcp_connection: dict[str, _FakeConn]) -> None:
    """The ``tools`` allow-list on MCPServerConfig must filter returned tools.

    Failure means all server tools are exposed regardless of the allow-list,
    breaking the policy goal of restricting which tools the agent can use.
    """
    patch_mcp_connection["github"] = _FakeConn(
        tools=[_make_tool("search"), _make_tool("delete_repo"), _make_tool("create_issue")]
    )
    # Only allow "search" — "delete_repo" and "create_issue" must be filtered out
    spec = _make_spec(_make_config("github", tools=["search"]))
    pool = ServerMcpPool()

    try:
        entries = await pool.list_tools("agent_1", spec)
    finally:
        await pool.shutdown_all()

    assert len(entries) == 1, (
        "Only the allowed tool should be returned; allow-list must filter others"
    )
    assert entries[0].tool.name == "search"


@pytest.mark.asyncio
async def test_list_tools_invalid_name_skipped(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """Tools with invalid names (containing spaces) must be silently skipped.

    Failure means the LLM provider rejects the schema because the tool name
    does not match the ``[a-zA-Z0-9_-]{1,256}`` constraint.
    """
    patch_mcp_connection["github"] = _FakeConn(
        tools=[_make_tool("valid_tool"), _make_tool("invalid tool name")]
    )
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    try:
        entries = await pool.list_tools("agent_1", spec)
    finally:
        await pool.shutdown_all()

    assert len(entries) == 1, (
        "Only the valid tool should be returned; invalid name must be skipped"
    )
    assert entries[0].tool.name == "valid_tool"


@pytest.mark.asyncio
async def test_list_tools_connect_failure_skips_server(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """A server that fails to connect must be skipped; healthy servers still surface tools.

    Failure means one unhealthy MCP server crashes the entire tool list,
    depriving the agent of all MCP tools because of one failing server.
    """
    patch_mcp_connection["good"] = _FakeConn(tools=[_make_tool("good_tool")])
    patch_mcp_connection["bad"] = _FakeConn(tools=[], connect_error=RuntimeError("unreachable"))
    spec = _make_spec(_make_config("good"), _make_config("bad"))
    pool = ServerMcpPool()

    try:
        entries = await pool.list_tools("agent_1", spec)
    finally:
        await pool.shutdown_all()

    tool_names = {e.tool.name for e in entries}
    assert tool_names == {"good_tool"}, (
        "Tools from the healthy server must surface even when another server fails"
    )


# ── call_tool ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_happy_path(patch_mcp_connection: dict[str, _FakeConn]) -> None:
    """``call_tool`` must route to the correct server and return its output.

    Failure means tool dispatch is broken and MCP tool calls never reach
    the external server.
    """
    patch_mcp_connection["github"] = _FakeConn(
        tools=[_make_tool("search")],
        call_tool_results={"search": "10 results found"},
    )
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    try:
        result = await pool.call_tool("agent_1", spec, "github", "search", {"q": "asyncio"})
    finally:
        await pool.shutdown_all()

    assert result == "10 results found", "call_tool must return the server's response verbatim"


@pytest.mark.asyncio
async def test_call_tool_no_mcp_servers_raises() -> None:
    """``call_tool`` must raise RuntimeError when the spec has no MCP servers.

    Failure means the pool silently swallows the dispatch error, leaving
    the harness with a None tool result.
    """
    pool = ServerMcpPool()
    with pytest.raises(RuntimeError, match="has no MCP servers"):
        await pool.call_tool("agent_1", _empty_spec(), "github", "search", {})
    await pool.shutdown_all()


@pytest.mark.asyncio
async def test_call_tool_unknown_server_raises(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """``call_tool`` must raise RuntimeError when the server name is not in the spec.

    Failure means dispatch silently falls through without calling any server,
    returning empty/None to the LLM.
    """
    patch_mcp_connection["github"] = _FakeConn(tools=[_make_tool("search")])
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    try:
        with pytest.raises(RuntimeError, match="no MCP server named"):
            await pool.call_tool("agent_1", spec, "nonexistent", "search", {})
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_call_tool_unhealthy_server_raises(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """``call_tool`` must raise RuntimeError when the server failed to connect.

    Failure means the pool tries to dispatch through a broken server connection,
    causing an opaque error deep in the MCP transport layer.
    """
    patch_mcp_connection["github"] = _FakeConn(
        tools=[], connect_error=ConnectionRefusedError("port closed")
    )
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    try:
        with pytest.raises(RuntimeError, match="unhealthy"):
            await pool.call_tool("agent_1", spec, "github", "search", {})
    finally:
        await pool.shutdown_all()


# ── shutdown_for / shutdown_all ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_for_removes_entry_and_closes(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """``shutdown_for`` must remove the agent's pool entry and close connections.

    Failure means connections leak when an agent is deleted — every
    subsequent agent deletion leaves an open MCP connection behind.
    """
    patch_mcp_connection["github"] = _FakeConn(tools=[_make_tool("search")])
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    # Warm the pool so there is a live connection to close
    await pool.list_tools("agent_1", spec)

    await pool.shutdown_for("agent_1")

    # 1. Entry must be gone
    assert ("agent_1", None) not in pool._entries, "Pool entry must be removed after shutdown_for"
    # 2. Connection must have been closed
    assert patch_mcp_connection["github"].close_calls == 1, (
        "close() must be called exactly once on the live connection; "
        "if 0, connections leak; if 2, double-close is a bug"
    )


@pytest.mark.asyncio
async def test_shutdown_for_noop_when_no_entry() -> None:
    """``shutdown_for`` must be a no-op when the agent has no pool entry.

    Failure means ``shutdown_for`` on an unknown agent raises, crashing the
    cleanup path on agent deletion.
    """
    pool = ServerMcpPool()
    # Should not raise
    await pool.shutdown_for("agent_that_never_existed")
    await pool.shutdown_all()


@pytest.mark.asyncio
async def test_shutdown_all_closes_all_entries(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """``shutdown_all`` must close every live connection and empty the pool.

    Failure means the Omnigent server shutdown leaks MCP connections, blocking
    graceful process exit.
    """
    patch_mcp_connection["gh"] = _FakeConn(tools=[_make_tool("search")])
    patch_mcp_connection["jira"] = _FakeConn(tools=[_make_tool("create_ticket")])

    spec_a = _make_spec(_make_config("gh"))
    spec_b = _make_spec(_make_config("jira"))
    pool = ServerMcpPool()

    await pool.list_tools("agent_a", spec_a)
    await pool.list_tools("agent_b", spec_b)

    await pool.shutdown_all()

    assert pool._entries == {}, "Pool must be empty after shutdown_all"
    assert patch_mcp_connection["gh"].close_calls == 1, (
        "gh connection must be closed once by shutdown_all"
    )
    assert patch_mcp_connection["jira"].close_calls == 1, (
        "jira connection must be closed once by shutdown_all"
    )


# ── spec hash invalidation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spec_hash_change_evicts_old_entry(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """Changing the spec's MCP servers must evict the old pool entry and reconnect.

    Failure means agents that get a new MCP server config keep using stale
    connections to the old servers — changes to the agent spec have no effect.
    """
    patch_mcp_connection["v1"] = _FakeConn(tools=[_make_tool("old_tool")])
    spec_v1 = _make_spec(_make_config("v1"))
    pool = ServerMcpPool()

    # Warm with the first spec version
    entries_v1 = await pool.list_tools("agent_1", spec_v1)
    assert {e.tool.name for e in entries_v1} == {"old_tool"}, "Sanity: v1 tools visible"

    # Now switch to a new spec (different MCP server name → different hash)
    patch_mcp_connection["v2"] = _FakeConn(tools=[_make_tool("new_tool")])
    spec_v2 = _make_spec(_make_config("v2"))

    entries_v2 = await pool.list_tools("agent_1", spec_v2)

    # Give the evict task a moment to close the old connection
    await asyncio.sleep(0)

    try:
        assert {e.tool.name for e in entries_v2} == {"new_tool"}, (
            "After spec hash change, new tools must be returned — old entry must have been evicted"
        )
        # The old connection was closed when the entry was evicted
        assert patch_mcp_connection["v1"].close_calls == 1, (
            "Old server connection must be closed when spec hash changes"
        )
    finally:
        await pool.shutdown_all()


# ── LRU eviction ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lru_evicts_least_recently_used_at_capacity(
    patch_mcp_connection: dict[str, _FakeConn],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the pool reaches capacity, the LRU agent's entry must be evicted.

    Failure means unbounded memory growth — each new agent adds connections
    without ever closing old ones, eventually exhausting resources.
    """
    # Shrink capacity to 2 so we can test eviction with 3 agents
    monkeypatch.setattr(_mcp_pool_module, "_POOL_AGENT_CAPACITY", 2)

    for name in ["a", "b", "c"]:
        patch_mcp_connection[name] = _FakeConn(tools=[_make_tool(f"{name}_tool")])

    pool = ServerMcpPool()

    spec_a = _make_spec(_make_config("a"))
    spec_b = _make_spec(_make_config("b"))
    spec_c = _make_spec(_make_config("c"))

    # Fill pool to capacity (agent_a then agent_b — agent_a is now LRU)
    await pool.list_tools("agent_a", spec_a)
    await pool.list_tools("agent_b", spec_b)

    # Adding agent_c must evict agent_a (the LRU)
    await pool.list_tools("agent_c", spec_c)

    # Give background evict tasks a cycle to complete
    await asyncio.sleep(0)

    assert ("agent_a", None) not in pool._entries, (
        "agent_a must be evicted (it was the LRU when agent_c was added)"
    )
    assert ("agent_b", None) in pool._entries, "agent_b must survive (not the LRU)"
    assert ("agent_c", None) in pool._entries, "agent_c must be present (just added)"
    assert len(pool._entries) == 2, "Pool must stay at capacity=2 after eviction"
    assert patch_mcp_connection["a"].close_calls == 1, (
        "agent_a's connection must be closed during LRU eviction; if 0, the connection leaked"
    )

    await pool.shutdown_all()


# ── user-scoped isolation & credential resolution ──────────────────────────


@pytest.mark.asyncio
async def test_per_user_pool_isolation(patch_mcp_connection: dict[str, _FakeConn]) -> None:
    """Two users on the same agent_id must get separate connections and tool scopes."""
    patch_mcp_connection["github:alice"] = _FakeConn(
        tools=[_make_tool("alice_tool")],
        call_tool_results={"who": "alice"},
    )
    patch_mcp_connection["github:bob"] = _FakeConn(
        tools=[_make_tool("bob_tool")],
        call_tool_results={"who": "bob"},
    )
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    try:
        alice_tools = await pool.list_tools("agent_1", spec, user_id="alice")
        bob_tools = await pool.list_tools("agent_1", spec, user_id="bob")
        assert {e.tool.name for e in alice_tools} == {"alice_tool"}
        assert {e.tool.name for e in bob_tools} == {"bob_tool"}

        alice_res = await pool.call_tool("agent_1", spec, "github", "who", {}, user_id="alice")
        bob_res = await pool.call_tool("agent_1", spec, "github", "who", {}, user_id="bob")
        assert alice_res == "alice"
        assert bob_res == "bob"

        assert ("agent_1", "alice") in pool._entries
        assert ("agent_1", "bob") in pool._entries
        assert patch_mcp_connection["github:alice"].connect_calls == 1
        assert patch_mcp_connection["github:bob"].connect_calls == 1
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_shutdown_for_user_scoped_and_whole_agent(
    patch_mcp_connection: dict[str, _FakeConn],
) -> None:
    """``shutdown_for(agent_id, user_id=...)`` closes only that user; ``None`` closes all."""
    patch_mcp_connection["github:alice"] = _FakeConn(tools=[_make_tool("search")])
    patch_mcp_connection["github:bob"] = _FakeConn(tools=[_make_tool("search")])
    spec = _make_spec(_make_config("github"))
    pool = ServerMcpPool()

    try:
        await pool.list_tools("agent_1", spec, user_id="alice")
        await pool.list_tools("agent_1", spec, user_id="bob")

        await pool.shutdown_for("agent_1", user_id="alice")
        assert ("agent_1", "alice") not in pool._entries
        assert ("agent_1", "bob") in pool._entries
        assert patch_mcp_connection["github:alice"].close_calls == 1
        assert patch_mcp_connection["github:bob"].close_calls == 0

        await pool.shutdown_for("agent_1")
        assert ("agent_1", "bob") not in pool._entries
        assert patch_mcp_connection["github:bob"].close_calls == 1
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_connection_capacity_lru_eviction_counts_connections(
    patch_mcp_connection: dict[str, _FakeConn],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity is measured in live connections across ``(agent_id, user_id)`` entries."""
    monkeypatch.setattr(_mcp_pool_module, "_POOL_CONNECTION_CAPACITY", 3)
    for key in ["s1:alice", "s2:alice", "s1:bob", "s1:carol"]:
        patch_mcp_connection[key] = _FakeConn(tools=[_make_tool("ping")])

    spec_two = _make_spec(_make_config("s1"), _make_config("s2"))
    spec_one = _make_spec(_make_config("s1"))
    pool = ServerMcpPool()

    try:
        await pool.list_tools("agent_1", spec_two, user_id="alice")  # 2 connections
        await pool.list_tools("agent_1", spec_one, user_id="bob")  # +1 = 3 connections
        # Adding carol's 1 connection exceeds 3 -> must evict alice's 2-connection entry.
        await pool.list_tools("agent_1", spec_one, user_id="carol")
        await asyncio.sleep(0)

        assert ("agent_1", "alice") not in pool._entries
        assert ("agent_1", "bob") in pool._entries
        assert ("agent_1", "carol") in pool._entries
        assert pool._live_connection_count() <= 3
        assert patch_mcp_connection["s1:alice"].close_calls == 1
        assert patch_mcp_connection["s2:alice"].close_calls == 1
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_pool_forwards_user_id_and_credential_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ServerMcpPool`` passes ``user_id``, resolver, and timeout to connections."""
    observed: list[tuple[str | None, Any, float | None]] = []

    class _CapturingConn:
        def __init__(
            self,
            *,
            config: MCPServerConfig,
            user_id: str | None = None,
            credential_resolver: Any = None,
            auth_command_timeout_s: float | None = None,
        ) -> None:
            observed.append((user_id, credential_resolver, auth_command_timeout_s))

        async def connect(self) -> list[McpToolDef]:
            return [_make_tool("ping")]

        async def close(self) -> None:
            return None

    monkeypatch.setattr("omnigent.server.mcp_pool.McpServerConnection", _CapturingConn)

    def _resolver(cfg: MCPServerConfig, uid: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer tok-{uid}"}

    pool = ServerMcpPool(credential_resolver=_resolver, auth_command_timeout_s=7.5)
    spec = _make_spec(_make_config("github"))
    try:
        await pool.list_tools("agent_1", spec, user_id="alice")
        await pool.list_tools("agent_1", spec, user_id="bob")
        assert observed == [
            ("alice", _resolver, 7.5),
            ("bob", _resolver, 7.5),
        ]
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_require_user_id_rejects_missing_user() -> None:
    """``require_user_id=True`` must reject missing or whitespace-only ``user_id``."""
    pool = ServerMcpPool(require_user_id=True)
    spec = _make_spec(_make_config("github"))
    with pytest.raises(ValueError, match="user_id is required"):
        await pool.list_tools("agent_1", spec)
    with pytest.raises(ValueError, match="user_id is required"):
        await pool.list_tools("agent_1", spec, user_id="   ")
    with pytest.raises(ValueError, match="user_id is required"):
        await pool.call_tool("agent_1", spec, "github", "search", {}, user_id=None)
    await pool.shutdown_all()


@pytest.mark.asyncio
async def test_shared_static_credentials_warns_and_refuses(
    patch_mcp_connection: dict[str, _FakeConn],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Multiple users on static-credential HTTP specs warn by default and refuse when strict."""
    patch_mcp_connection["github"] = _FakeConn(tools=[_make_tool("search")])
    static_cfg = MCPServerConfig(
        name="github",
        transport="http",
        url="https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer shared-secret"},
    )
    spec_static = _make_spec(static_cfg)

    warn_pool = ServerMcpPool(shared_credential_policy="warn")
    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.server.mcp_pool"):
            await warn_pool.list_tools("agent_1", spec_static, user_id="alice")
            assert not any("shares static MCP credentials" in r.message for r in caplog.records)
            await warn_pool.list_tools("agent_1", spec_static, user_id="bob")
            assert any("shares static MCP credentials" in r.message for r in caplog.records)
    finally:
        await warn_pool.shutdown_all()

    refuse_pool = ServerMcpPool(shared_credential_policy="refuse")
    try:
        await refuse_pool.list_tools("agent_1", spec_static, user_id="alice")
        with pytest.raises(RuntimeError, match="Refusing to share static MCP credentials"):
            await refuse_pool.list_tools("agent_1", spec_static, user_id="bob")
    finally:
        await refuse_pool.shutdown_all()

    # With per-user auth_command, strict refuse mode allows distinct users without warning.
    dynamic_cfg = MCPServerConfig(
        name="github",
        transport="http",
        url="https://mcp.example.com/mcp",
        auth_command="printf %s tok",
    )
    strict_ok_pool = ServerMcpPool(shared_credential_policy="refuse")
    try:
        await strict_ok_pool.list_tools("agent_1", _make_spec(dynamic_cfg), user_id="alice")
        await strict_ok_pool.list_tools("agent_1", _make_spec(dynamic_cfg), user_id="bob")
    finally:
        await strict_ok_pool.shutdown_all()
