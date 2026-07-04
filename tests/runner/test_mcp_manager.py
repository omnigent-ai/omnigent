"""Unit tests for :class:`RunnerMcpManager`.

Cover invariants that survived the move from server-side ToolManager
to runner-side ownership (designs/RUNNER_MCP.md):

- partial failure: one MCP fails, others still surface their schemas
- spec-hash pool reuse: same spec across calls shares one connection
  per server (only one connect per server lifetime)
- per-server pool reuse: different specs sharing the same server config
  reuse one connection while applying their own names/tool filters
- invalid-name filtering: tools whose names violate the LLM-call
  constraint never reach the schema list
- tool-name collision: two MCPs in the same spec exposing the same
  name produce well-defined dispatch behavior

Plus new runner-side invariants the refactor introduced: LRU pool
eviction at capacity, spawn-free prewarm registration, and on-demand
connect cancellation on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import pytest
from mcp.types import Tool as McpToolDef

from omnigent.runner import mcp_manager as _mcp_manager_module
from omnigent.runner.mcp_manager import (
    _POOL_SPEC_CAPACITY,
    McpSchemasResult,
    RunnerMcpManager,
    compute_server_hash,
    compute_spec_hash,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig


def _make_spec(*configs: MCPServerConfig) -> AgentSpec:
    """AgentSpec with the given MCPServerConfigs and nothing else."""
    return AgentSpec(spec_version=1, name="test-agent", mcp_servers=list(configs))


def _make_config(name: str) -> MCPServerConfig:
    """HTTP MCPServerConfig."""
    return MCPServerConfig(
        name=name,
        transport="http",
        url=f"http://localhost/{name}",
    )


def _make_tool_def(name: str, description: str = "test tool") -> McpToolDef:
    """McpToolDef with a minimal valid inputSchema."""
    return McpToolDef(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
    )


# ── Helpers to stub McpServerConnection without spawning subprocesses ──


class _FakeConn:
    """Stand-in for McpServerConnection used by the patched connect.

    Records connect / close to let tests assert lifecycle.
    """

    def __init__(self, tools: list[McpToolDef]) -> None:
        self._tools = tools
        self.connect_calls = 0
        self.close_calls = 0
        self.call_tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> list[McpToolDef]:
        """Mark connect; return the canned tool list."""
        self.connect_calls += 1
        return self._tools

    async def close(self) -> None:
        """Mark close."""
        self.close_calls += 1

    async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
        """Record the invocation; return a deterministic stub."""
        self.call_tool_calls.append((name, arguments))
        return f"called {name} with {arguments}"


@pytest.fixture()
def patch_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, _FakeConn]:
    """Patch ``McpServerConnection`` so each instance is a recordable _FakeConn.

    Returns a dict keyed by config.name → _FakeConn so each test can
    decide per-server behavior (success vs raise) before calling
    ``schemas_for``.
    """
    conns: dict[str, _FakeConn] = {}
    raise_for: dict[str, Exception] = {}
    tools_for: dict[str, list[McpToolDef]] = {}

    class _PatchedConn:
        """Standin for ``McpServerConnection`` that pulls scripted behavior from the closure."""

        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            """Record the spawn; record connect/close on a per-config _FakeConn."""
            self._config = config
            self._inner = _FakeConn(tools_for.get(config.name, []))
            conns[config.name] = self._inner

        async def connect(self) -> list[McpToolDef]:
            """Surface either a scripted error or the canned tool list."""
            if self._config.name in raise_for:
                raise raise_for[self._config.name]
            return await self._inner.connect()

        async def close(self) -> None:
            """Forward close to the underlying _FakeConn for assertion."""
            await self._inner.close()

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            """Forward to the per-config _FakeConn so tests can assert dispatch."""
            return await self._inner.call_tool(name, arguments)

    monkeypatch.setattr(
        "omnigent.runner.mcp_manager.McpServerConnection",
        _PatchedConn,
    )
    # Tests script per-server behavior by mutating these dicts BEFORE
    # calling schemas_for; the closure above honors the script.
    conns["__raise_for__"] = raise_for  # type: ignore[assignment]
    conns["__tools_for__"] = tools_for  # type: ignore[assignment]
    return conns


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_failure_surfaces_healthy_servers(
    patch_connection: dict[str, Any],
) -> None:
    """One MCP failing at connect must not poison the others.

    Schemas from the healthy server appear; the broken one shows up
    in ``failures`` only.
    """
    patch_connection["__tools_for__"]["good"] = [_make_tool_def("good_tool")]
    patch_connection["__raise_for__"]["bad"] = RuntimeError("upstream down")

    spec = _make_spec(_make_config("good"), _make_config("bad"))
    manager = RunnerMcpManager()
    try:
        result = await manager.schemas_for(spec)
    finally:
        await manager.shutdown()

    assert result.tool_names == {"good__good_tool"}, (
        "Healthy server's tool must surface even though 'bad' failed"
    )
    assert "bad" in result.failures, "Broken server must appear in failures"
    assert "upstream down" in result.failures["bad"]
    assert "good" not in result.failures


@pytest.mark.asyncio
async def test_pool_reuses_connection_for_same_spec(
    patch_connection: dict[str, Any],
) -> None:
    """schemas_for called twice with the same spec connects each server once.

    Spec-hash keying means a runner serving many conversations against
    the same agent pays MCP spawn cost only once per server.
    """
    patch_connection["__tools_for__"]["jira"] = [_make_tool_def("jira_search")]
    spec = _make_spec(_make_config("jira"))

    manager = RunnerMcpManager()
    try:
        first = await manager.schemas_for(spec)
        second = await manager.schemas_for(spec)
    finally:
        await manager.shutdown()

    assert first.tool_names == second.tool_names == {"jira__jira_search"}
    # Single connect call across both schemas_for invocations proves
    # the pool reuses the connection instead of respawning.
    assert patch_connection["jira"].connect_calls == 1, (
        f"expected 1 connect, got {patch_connection['jira'].connect_calls}"
    )


@pytest.mark.asyncio
async def test_pool_separate_entries_for_different_specs(
    patch_connection: dict[str, Any],
) -> None:
    """Two specs with different MCP configs get independent pool entries.

    Same MCP server NAME, different command/args → different spec_hash →
    different pool entry → two connect calls.
    """
    patch_connection["__tools_for__"]["jira"] = [_make_tool_def("jira_search")]

    spec_a = AgentSpec(
        spec_version=1,
        name="agent-a",
        mcp_servers=[
            MCPServerConfig(
                name="jira",
                transport="stdio",
                command="python",
                args=["-m", "jira_v1"],
            )
        ],
    )
    spec_b = AgentSpec(
        spec_version=1,
        name="agent-b",
        mcp_servers=[
            MCPServerConfig(
                name="jira",
                transport="stdio",
                command="python",
                args=["-m", "jira_v2"],
            )
        ],
    )

    assert compute_spec_hash(spec_a.mcp_servers) != compute_spec_hash(spec_b.mcp_servers)

    manager = RunnerMcpManager()
    try:
        await manager.schemas_for(spec_a)
        await manager.schemas_for(spec_b)
        # Snapshot before shutdown — shutdown() clears the pool.
        snapshot = manager.status_snapshot()
    finally:
        await manager.shutdown()

    assert len(snapshot["specs"]) == 2, (
        f"expected 2 pool entries (one per spec_hash), got {len(snapshot['specs'])}"
    )


@pytest.mark.asyncio
async def test_shared_server_reused_across_different_specs(
    patch_connection: dict[str, Any],
) -> None:
    """Different spec hashes share one connection for identical server config.

    The per-spec ``tools`` allow-list still filters schemas independently,
    but it no longer forces another stdio/http MCP process.
    """
    patch_connection["__tools_for__"]["jira"] = [
        _make_tool_def("search"),
        _make_tool_def("create"),
    ]
    cfg_search = _make_config("jira")
    cfg_search.tools = ["search"]
    cfg_create = _make_config("jira")
    cfg_create.tools = ["create"]
    spec_search = _make_spec(cfg_search)
    spec_create = _make_spec(cfg_create)

    assert compute_spec_hash(spec_search.mcp_servers) != compute_spec_hash(spec_create.mcp_servers)
    assert compute_server_hash(cfg_search) == compute_server_hash(cfg_create)

    manager = RunnerMcpManager()
    try:
        first = await manager.schemas_for(spec_search)
        second = await manager.schemas_for(spec_create)
        snapshot = manager.status_snapshot()
    finally:
        await manager.shutdown()

    assert first.tool_names == {"jira__search"}
    assert second.tool_names == {"jira__create"}
    assert patch_connection["jira"].connect_calls == 1
    assert len(snapshot["specs"]) == 2


@pytest.mark.asyncio
async def test_invalid_tool_name_is_filtered(
    patch_connection: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tools whose names violate ``^[a-zA-Z0-9_-]{1,256}$`` are filtered.

    LLM providers reject these at API call time with unhelpful errors;
    filter at schema-injection time and log a warning instead.
    """
    # Force the `omnigent` package logger to propagate so caplog
    # captures warnings. Defensive: ``omnigent.cli_diagnostics
    # .setup_cli_logging`` sets ``omnigent.propagate = False`` and
    # if a sibling test on this xdist worker invoked it via a fixture
    # that didn't tear down (e.g. crash mid-test), the False sticks
    # for the rest of the worker — caplog's root handler then misses
    # every ``omnigent.*`` warning.
    logging.getLogger("omnigent").propagate = True
    patch_connection["__tools_for__"]["jira"] = [
        _make_tool_def("ok_tool"),
        _make_tool_def("bad name with spaces"),
        _make_tool_def("bad:colon"),
        _make_tool_def(""),
        _make_tool_def("a" * 257),
    ]
    spec = _make_spec(_make_config("jira"))
    manager = RunnerMcpManager()
    # Capture at the source logger directly. Another test in the same
    # xdist worker may have set ``omnigent.propagate = False`` (the CLI
    # diagnostics setup does), which severs propagation to the root logger
    # where caplog listens — capturing nothing. Attaching caplog's handler
    # to the mcp_manager logger makes capture independent of the propagate
    # chain, so the assertion below is robust to that state leak.
    mcp_logger = logging.getLogger("omnigent.runner.mcp_manager")
    mcp_logger.addHandler(caplog.handler)
    try:
        # Bare ``at_level(WARNING)`` — no ``logger=`` arg. We've forced
        # propagation above, so records reach the root logger where
        # caplog's handler lives. Passing ``logger="omnigent.runner.
        # mcp_manager"`` here in addition would double-attach the
        # handler (root + that named logger), capturing each warning
        # twice.
        with caplog.at_level(logging.WARNING):
            result = await manager.schemas_for(spec)
    finally:
        mcp_logger.removeHandler(caplog.handler)
        await manager.shutdown()

    # Only the valid name survives; the four invalid ones are dropped.
    assert result.tool_names == {"jira__ok_tool"}, (
        f"expected only jira__ok_tool to survive; got {result.tool_names}"
    )
    # Each bad name produced its own warning so operators can fix the MCP.
    # Dedupe by message text: under pytest-xdist + caplog with leaked
    # handler attachments from sibling tests on the same worker, the
    # same record can be captured by multiple handlers. We care about
    # the set of distinct warnings, not the multiplicity of captures.
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    skipped_lines = {m for m in warning_msgs if "invalid name" in m}
    assert len(skipped_lines) == 4, (
        f"expected 4 distinct invalid-name warnings, got {len(skipped_lines)}: "
        f"{sorted(skipped_lines)}"
    )


@pytest.mark.asyncio
async def test_tool_name_collision_first_server_wins_dispatch(
    patch_connection: dict[str, Any],
) -> None:
    """Two MCPs exposing the same bare tool name: namespacing prevents collision.

    After the ``{server}__{tool}`` namespacing refactor, both servers surface
    their own prefixed name (``jira-a__get_issue`` and ``jira-b__get_issue``),
    so there is no collision.  Dispatch is unambiguous: the namespaced name
    selects the owning server.
    """
    patch_connection["__tools_for__"]["jira-a"] = [_make_tool_def("get_issue")]
    patch_connection["__tools_for__"]["jira-b"] = [_make_tool_def("get_issue")]

    spec = _make_spec(_make_config("jira-a"), _make_config("jira-b"))
    manager = RunnerMcpManager()
    try:
        result = await manager.schemas_for(spec)
        assert result.tool_names == {"jira-a__get_issue", "jira-b__get_issue"}, (
            "Namespacing must surface both servers' tools without collision"
        )
        output = await manager.call_tool(spec, "jira-a__get_issue", {"key": "K-1"})
    finally:
        await manager.shutdown()

    # The namespaced call routes to the correct server.
    assert patch_connection["jira-a"].call_tool_calls == [("get_issue", {"key": "K-1"})]
    assert patch_connection["jira-b"].call_tool_calls == [], (
        "jira-b must not receive a dispatch routed to jira-a"
    )
    assert "called get_issue" in output


@pytest.mark.asyncio
async def test_call_tool_rejects_wrong_namespaced_server(
    patch_connection: dict[str, Any],
) -> None:
    """A stale or wrong server prefix must not dispatch by bare tool name."""
    patch_connection["__tools_for__"]["jira-a"] = [_make_tool_def("get_issue")]

    spec = _make_spec(_make_config("jira-a"))
    manager = RunnerMcpManager()
    try:
        await manager.schemas_for(spec)
        with pytest.raises(RuntimeError, match="no live MCP serving tool"):
            await manager.call_tool(spec, "jira-old__get_issue", {"key": "K-1"})
    finally:
        await manager.shutdown()

    assert patch_connection["jira-a"].call_tool_calls == []


@pytest.mark.asyncio
async def test_call_tool_connects_only_target_namespaced_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct namespaced dispatch connects the addressed MCP only."""
    connected: list[str] = []
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class _TargetedConn:
        """Connection stub that records which server was connected."""

        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            """Record config for connect/call assertions."""
            self._config = config

        async def connect(self) -> list[McpToolDef]:
            """Expose one tool whose name is shared across servers."""
            connected.append(self._config.name)
            return [_make_tool_def("run")]

        async def close(self) -> None:
            """No-op."""

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            """Record the call and return a stub output."""
            calls.append((self._config.name, name, arguments))
            return "ok"

    monkeypatch.setattr(_mcp_manager_module, "McpServerConnection", _TargetedConn)

    spec = _make_spec(_make_config("used"), _make_config("unused"))
    manager = RunnerMcpManager()
    try:
        output = await manager.call_tool(spec, "used__run", {"x": 1})
    finally:
        await manager.shutdown()

    assert output == "ok"
    assert connected == ["used"]
    assert calls == [("used", "run", {"x": 1})]


@pytest.mark.asyncio
async def test_call_tool_against_failed_server_raises(
    patch_connection: dict[str, Any],
) -> None:
    """call_tool against a tool whose owning MCP failed surfaces a clear error.

    Lets the LLM see ``Error: ...`` instead of hanging or silently
    returning empty content.
    """
    patch_connection["__raise_for__"]["jira"] = RuntimeError("upstream down")
    spec = _make_spec(_make_config("jira"))
    manager = RunnerMcpManager()
    try:
        await manager.schemas_for(spec)
        with pytest.raises(RuntimeError, match="no live MCP serving tool"):
            await manager.call_tool(spec, "jira_search_issues", {})
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_empty_mcp_servers_short_circuits(
    patch_connection: dict[str, Any],
) -> None:
    """Specs with no MCP servers don't trigger any pool work.

    No connection spawned, no pool entry created, no failures.
    """
    spec = AgentSpec(spec_version=1, name="bare-agent", mcp_servers=[])
    manager = RunnerMcpManager()
    try:
        result = await manager.schemas_for(spec)
    finally:
        await manager.shutdown()

    assert result == McpSchemasResult(schemas=[], tool_names=set(), failures={})
    # Sanity: nothing landed in the pool either.
    assert manager.status_snapshot() == {"specs": []}


@pytest.mark.asyncio
async def test_lru_eviction_at_pool_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (capacity+1)-th spec evicts the LRU and closes its connections.

    Guards against unbounded process growth on long-running runners that
    serve many distinct agent specs.
    """
    closed_names: list[str] = []

    class _CountingConn:
        """Standin for McpServerConnection that records close()."""

        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            """Bind to the config so close() records the server name."""
            self._config = config

        async def connect(self) -> list[McpToolDef]:
            """Return one tool so the pool entry counts as ready."""
            return [_make_tool_def(f"{self._config.name}_tool")]

        async def close(self) -> None:
            """Record close so the test can assert eviction-driven cleanup."""
            closed_names.append(self._config.name)

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            """Unused in this test but required by the interface."""
            return ""

    monkeypatch.setattr(_mcp_manager_module, "McpServerConnection", _CountingConn)

    manager = RunnerMcpManager()
    # Build capacity + 1 distinct specs (different server.name → different
    # spec_hash → distinct pool entry). First spec is the LRU and should
    # be evicted when the (capacity+1)-th arrives.
    specs: list[AgentSpec] = []
    for i in range(_POOL_SPEC_CAPACITY + 1):
        specs.append(_make_spec(_make_config(f"server-{i}")))
    try:
        for spec in specs:
            await manager.schemas_for(spec)
        # Let the fire-and-forget evict-close tasks run.
        await asyncio.sleep(0.05)
        snapshot_specs = manager.status_snapshot()["specs"]
    finally:
        await manager.shutdown()

    # Pool stays at capacity after eviction.
    assert len(snapshot_specs) == _POOL_SPEC_CAPACITY, (
        f"pool should clamp at {_POOL_SPEC_CAPACITY}; got {len(snapshot_specs)}"
    )
    # The very first spec was evicted; the rest survived.
    surviving_servers = {s["servers"][0]["name"] for s in snapshot_specs}
    assert "server-0" not in surviving_servers, "LRU spec must be evicted"
    # Eviction closed the LRU's connection.
    assert "server-0" in closed_names, (
        f"evicted spec's MCP must be closed; close()d names: {closed_names}"
    )


@pytest.mark.asyncio
async def test_prewarm_registers_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prewarm() records routing metadata but does not spawn MCP processes."""
    connect_calls = 0

    class _CountingConn:
        """Connection that records when an actual spawn/connect happens."""

        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            """Record the config for the returned tool name."""
            self._config = config

        async def connect(self) -> list[McpToolDef]:
            """Count connect calls and return one tool."""
            nonlocal connect_calls
            connect_calls += 1
            return [_make_tool_def("t")]

        async def close(self) -> None:
            """No-op; unused here."""

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            """Unused in this test."""
            return ""

    monkeypatch.setattr(_mcp_manager_module, "McpServerConnection", _CountingConn)

    spec = _make_spec(_make_config("slow"))
    manager = RunnerMcpManager()
    try:
        await manager.prewarm(spec)
        await manager.prewarm(spec)
        snapshot = manager.status_snapshot()
        assert connect_calls == 0

        result = await manager.schemas_for(spec)
    finally:
        await manager.shutdown()

    assert snapshot["specs"][0]["servers"][0]["status"] == "pending"
    assert result.tool_names == {"slow__t"}
    assert connect_calls == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_in_flight_schema_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown() cancels on-demand connect tasks that never completed.

    Otherwise the runner would leak background tasks holding refs to
    half-opened MCP transports.
    """
    started = asyncio.Event()
    never_release = asyncio.Event()  # intentionally never set

    class _HangingConn:
        """Connection whose connect() never completes."""

        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            """Capture config (unused)."""
            self._config = config

        async def connect(self) -> list[McpToolDef]:
            """Signal start, then wait forever (until cancelled)."""
            started.set()
            await never_release.wait()
            return []  # pragma: no cover — cancelled before reaching

        async def close(self) -> None:
            """No-op."""

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            """Unused."""
            return ""

    monkeypatch.setattr(_mcp_manager_module, "McpServerConnection", _HangingConn)

    spec = _make_spec(_make_config("hangs"))
    manager = RunnerMcpManager()
    schemas_task = asyncio.create_task(manager.schemas_for(spec))
    await started.wait()
    # Capture the shared-server connect task ref before shutdown clears it.
    server_hash = compute_server_hash(spec.mcp_servers[0])
    connect_task = manager._servers[server_hash].connect_task
    assert connect_task is not None and not connect_task.done(), (
        "connect task must be in flight before shutdown"
    )

    await manager.shutdown()
    # Give the cancellation a tick to propagate.
    await asyncio.sleep(0.01)

    assert connect_task.cancelled() or connect_task.done(), (
        "shutdown must cancel (or otherwise finalize) the in-flight connect task"
    )
    schemas_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await schemas_task


@pytest.mark.asyncio
async def test_evicted_spec_closes_late_completed_shared_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that finishes after spec eviction is still closed."""
    started = asyncio.Event()
    finish = asyncio.Event()
    closed: list[str] = []

    class _SlowConn:
        """Connection whose connect can be completed after LRU eviction."""

        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            self._config = config

        async def connect(self) -> list[McpToolDef]:
            started.set()
            await finish.wait()
            return [_make_tool_def("run")]

        async def close(self) -> None:
            closed.append(self._config.name)

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            return "ok"

    monkeypatch.setattr(_mcp_manager_module, "McpServerConnection", _SlowConn)

    spec = _make_spec(_make_config("shared"))
    server_hash = compute_server_hash(spec.mcp_servers[0])
    manager = RunnerMcpManager()
    call_task = asyncio.create_task(manager.call_tool(spec, "shared__run", {}))
    try:
        await started.wait()
        for i in range(_POOL_SPEC_CAPACITY):
            await manager.prewarm(_make_spec(_make_config(f"evict-{i}")))

        assert server_hash in manager._servers, "active call must keep the shared server tracked"
        finish.set()
        assert await call_task == "ok"
        await asyncio.sleep(0.05)

        assert server_hash not in manager._servers
        assert closed == ["shared"]
    finally:
        if not call_task.done():
            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task
        await manager.shutdown()


@pytest.mark.asyncio
async def test_shared_connect_survives_one_spec_eviction_while_referenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evicting one spec must not cancel a connect another spec still needs."""
    started = asyncio.Event()
    finish = asyncio.Event()
    connect_calls = 0

    class _SlowConn:
        """Shared connection that records connect count."""

        def __init__(self, *, config: MCPServerConfig, cwd: Any = None, **_kwargs: Any) -> None:
            self._config = config

        async def connect(self) -> list[McpToolDef]:
            nonlocal connect_calls
            connect_calls += 1
            started.set()
            await finish.wait()
            return [_make_tool_def("run"), _make_tool_def("other")]

        async def close(self) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            return "ok"

    monkeypatch.setattr(_mcp_manager_module, "McpServerConnection", _SlowConn)

    cfg_run = _make_config("shared")
    cfg_run.tools = ["run"]
    cfg_other = _make_config("shared")
    cfg_other.tools = ["other"]
    spec_run = _make_spec(cfg_run)
    spec_other = _make_spec(cfg_other)

    manager = RunnerMcpManager()
    call_task = asyncio.create_task(manager.call_tool(spec_run, "shared__run", {}))
    try:
        await started.wait()
        await manager.prewarm(spec_other)
        for i in range(_POOL_SPEC_CAPACITY - 1):
            await manager.prewarm(_make_spec(_make_config(f"evict-other-{i}")))

        finish.set()
        assert await call_task == "ok"
        schemas = await manager.schemas_for(spec_other)

        assert schemas.tool_names == {"shared__other"}
        assert connect_calls == 1
    finally:
        if not call_task.done():
            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task
        await manager.shutdown()


@pytest.mark.asyncio
async def test_tool_allowlist_blocks_dispatch(
    patch_connection: dict[str, Any],
) -> None:
    """Per-spec tool filters apply to dispatch, not just schema listing."""
    patch_connection["__tools_for__"]["jira"] = [
        _make_tool_def("search"),
        _make_tool_def("create"),
    ]
    cfg = _make_config("jira")
    cfg.tools = ["search"]
    spec = _make_spec(cfg)
    manager = RunnerMcpManager()
    try:
        schemas = await manager.schemas_for(spec)
        with pytest.raises(RuntimeError, match="no live MCP serving tool"):
            await manager.call_tool(spec, "jira__create", {})
        output = await manager.call_tool(spec, "jira__search", {"q": "one"})
    finally:
        await manager.shutdown()

    assert schemas.tool_names == {"jira__search"}
    assert output == "called search with {'q': 'one'}"
    assert patch_connection["jira"].call_tool_calls == [("search", {"q": "one"})]


@pytest.mark.asyncio
async def test_stdio_cwd_threaded_to_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stdio_cwd`` reaches ``McpServerConnection(cwd=...)`` and the spec hash.

    Relative ``command`` paths in YAML (``.venv/bin/python``) resolve
    against this cwd. Different cwds produce different spec hashes so
    a runner serving two workspaces doesn't share pool entries.
    """
    received_cwds: list[Path | None] = []

    class _CwdRecorder:
        """Standin that records the cwd kwarg on construction."""

        def __init__(
            self,
            *,
            config: MCPServerConfig,
            cwd: Path | None = None,
            **_kw: Any,
        ) -> None:
            """Record cwd for the assertion."""
            received_cwds.append(cwd)
            self._config = config

        async def connect(self) -> list[McpToolDef]:
            """Return one tool so the pool entry counts as ready."""
            return [_make_tool_def(f"{self._config.name}_t")]

        async def close(self) -> None:
            """No-op."""

        async def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> str:
            """Unused."""
            return ""

    monkeypatch.setattr(_mcp_manager_module, "McpServerConnection", _CwdRecorder)

    spec = _make_spec(_make_config("jira"))
    project = Path("/tmp/some-project-root")
    other = Path("/tmp/other-project-root")

    manager_a = RunnerMcpManager(stdio_cwd=project)
    try:
        await manager_a.schemas_for(spec)
    finally:
        await manager_a.shutdown()

    manager_b = RunnerMcpManager(stdio_cwd=other)
    try:
        await manager_b.schemas_for(spec)
    finally:
        await manager_b.shutdown()

    assert received_cwds == [project, other], (
        f"each manager must thread its own stdio_cwd; got {received_cwds}"
    )
    # Spec hash must differ across cwds so the pools don't alias.
    assert compute_spec_hash(spec.mcp_servers, project) != compute_spec_hash(
        spec.mcp_servers, other
    )


def test_strip_mcp_tool_prefix_preserves_bare_double_underscore() -> None:
    """``_strip_mcp_tool_prefix`` only strips ``mcp__<server>__`` shape.

    Regression: the old impl ``rsplit("__", 1)[-1]`` clobbered any tool
    name with a ``__`` in it (e.g. ``my__weird_tool`` → ``weird_tool``).
    Only strip Claude-SDK MCP-prefixed names; pass everything else
    through.
    """
    from omnigent.runtime.workflow import _strip_mcp_tool_prefix

    # Claude-SDK shape: stripped to the bare name.
    assert _strip_mcp_tool_prefix("mcp__jira__jira_search_issues") == "jira_search_issues"
    # Bare names with double-underscore: preserved verbatim.
    assert _strip_mcp_tool_prefix("my__tool") == "my__tool"
    assert _strip_mcp_tool_prefix("bare_name") == "bare_name"
    # Looks-like-prefix but only two parts: preserved.
    assert _strip_mcp_tool_prefix("mcp__missing_third") == "mcp__missing_third"
