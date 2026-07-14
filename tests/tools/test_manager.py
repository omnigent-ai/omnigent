"""Tests for omnigent.tools.manager (ToolManager)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.errors import OmnigentError
from omnigent.spec.types import (
    AgentSpec,
    BuiltinToolConfig,
    LLMConfig,
    LocalToolInfo,
    MCPServerConfig,
    SharePolicy,
    SkillSpec,
    ToolRuntime,
    ToolsConfig,
)
from omnigent.tools import ToolManager
from omnigent.tools.base import ToolContext
from omnigent.tools.client_specified import ClientSideTool, ClientSideToolSpec
from omnigent.tools.mcp import clear_discovery_cache

_TEST_CTX = ToolContext(task_id="task_test", agent_id="agent_test")

# Tools that ToolManager registers unconditionally on every
# spec these tests construct: the lifecycle ``sys_cancel_task``
# plus the async-inbox trio (``sys_call_async``,
# ``sys_read_inbox``, ``sys_cancel_async``) which register
# whenever ``async_enabled`` is True (the default in
# ``_make_spec``). Filtered out in tests that assert on
# *specific* tool sets so these don't alter the other checks.
# Adding a tool here requires a deliberate decision — this is
# a hard-coded allowlist, not a filter on ``_register_*``.
# ``check_task`` was dropped per design step 11.
_ALWAYS_PRESENT_TOOLS: frozenset[str] = frozenset(
    {
        "sys_cancel_task",
        "sys_call_async",
        "sys_read_inbox",
        "sys_cancel_async",
        # ``load_skill`` is always registered — it discovers
        # host-scope skills at init time even when the agent
        # has no bundled skills.
        "load_skill",
        # Comment tools are always auto-registered so agents can
        # list and update review comments without the spec opting in.
        "list_comments",
        "update_comment",
        # Read-only session discovery tools are registered for every
        # agent (a user-added agent that declares no sub-agents still
        # needs to list/peek/get-info on its session-mates); the
        # mutating tools (sys_session_send/close/create/share) are
        # opt-in via tools.agents or the top-level ``spawn: true``.
        "sys_session_get_history",
        "sys_session_list",
        "sys_session_get_info",
        # Read-only agent discovery tools are likewise always available
        # (global, permission-bounded reads of any accessible session's
        # agent / bundle).
        "sys_agent_get",
        "sys_agent_download",
        "sys_agent_list",
        # Policy tools are always auto-registered so agents can
        # browse the registry and add policies at runtime.
        "sys_add_policy",
        "sys_policy_registry",
        # Embedded-browser tools are always auto-registered (framework-
        # owned) so any agent can drive the desktop app's browser without
        # the spec opting in. Schema-only; runner-dispatched.
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    }
)


def _non_lifecycle_schemas(
    mgr: ToolManager,
) -> list[dict[str, object]]:
    """
    Return tool schemas minus the always-registered lifecycle
    tool (``sys_cancel_task``).

    Tests that assert "no tools registered" or "only tool X"
    should filter out the lifecycle tools — they're part of
    every ToolManager but orthogonal to whatever the test is
    actually checking.

    :param mgr: The :class:`ToolManager` under test.
    :returns: Schema list with lifecycle entries filtered out.
    """
    return [
        s
        for s in mgr.get_tool_schemas()
        if not (
            isinstance(s, dict)
            and isinstance(fn := s.get("function"), dict)
            and fn.get("name") in _ALWAYS_PRESENT_TOOLS
        )
    ]


@pytest.fixture()
def skill_with_resources(tmp_path: Path) -> SkillSpec:
    """
    A skill with a ``references/`` directory containing a
    file, for testing ``read_skill_file`` registration.

    :returns: A ``SkillSpec`` pointing at a real directory
        with a reference file.
    """
    skill_dir = tmp_path / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "style-guide.md").write_text("# Style Guide\n\nUse snake_case.")
    return SkillSpec(
        name="code-review",
        description="Reviews code.",
        content="Review the code.",
        skill_dir=skill_dir,
    )


@pytest.fixture()
def skill_no_resources() -> SkillSpec:
    """
    A skill with no ``skill_dir`` (in-memory only).

    :returns: A ``SkillSpec`` with ``skill_dir=None``.
    """
    return SkillSpec(
        name="summarize",
        description="Summarizes text.",
        content="Summarize the input concisely.",
    )


@pytest.fixture(autouse=True)
def _clean_mcp_cache() -> None:
    """
    Clear the MCP discovery cache before each test.
    """
    clear_discovery_cache()


def _make_spec(
    skills: list[SkillSpec] | None = None,
    mcp_servers: list[MCPServerConfig] | None = None,
    local_tools: list[LocalToolInfo] | None = None,
) -> AgentSpec:
    """
    Create a minimal ``AgentSpec`` with the given skills,
    MCP servers, and local tools.

    :param skills: Skills to include, or ``None`` for no
        skills.
    :param mcp_servers: MCP server configs, or ``None`` for
        no MCP servers.
    :param local_tools: Local tool infos, or ``None`` for
        no local tools.
    :returns: An ``AgentSpec`` with ``spec_version=1``.
    """
    return AgentSpec(
        spec_version=1,
        skills=skills or [],
        mcp_servers=mcp_servers or [],
        local_tools=local_tools or [],
    )


# ── Registry dispatch ─────────────────────────────────


def test_registry_dispatches_to_load_skill(
    skill_no_resources: SkillSpec,
) -> None:
    """
    ToolManager.call_tool dispatches to LoadSkillTool via
    the registry.
    """
    mgr = ToolManager(
        _make_spec([skill_no_resources]),
    )
    result = mgr.call_tool(
        "load_skill",
        json.dumps({"name": "summarize"}),
        _TEST_CTX,
    )
    assert result == "Summarize the input concisely."


def test_registry_dispatches_to_read_skill_file(
    skill_with_resources: SkillSpec,
) -> None:
    """
    ToolManager.call_tool dispatches to ReadSkillFileTool
    via the registry.
    """
    mgr = ToolManager(
        _make_spec([skill_with_resources]),
    )
    result = mgr.call_tool(
        "read_skill_file",
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "references/style-guide.md",
            }
        ),
        _TEST_CTX,
    )
    assert "# Style Guide" in result


def test_registry_unknown_tool_returns_error(
    skill_no_resources: SkillSpec,
) -> None:
    """
    ToolManager.call_tool returns error for unregistered tools.
    """
    mgr = ToolManager(
        _make_spec([skill_no_resources]),
    )
    result = mgr.call_tool("nonexistent", json.dumps({}), _TEST_CTX)
    assert "not found" in result
    assert "load_skill" in result


# ── get_tool_schemas ──────────────────────────────────


def test_schemas_include_load_skill_when_skills_exist(
    skill_no_resources: SkillSpec,
) -> None:
    """
    get_tool_schemas includes load_skill when the agent has
    skills, and the schema description enumerates the available
    skill names.

    The enumeration is the discovery mechanism for harnesses with no
    native skill loader (openai-agents-sdk, and the in-process loop):
    they receive ``load_skill`` via ``request.tools`` and learn which
    skills exist from its description alone — they do not depend on the
    prompt's skill listing. If the enumeration regressed, those harnesses
    could call ``load_skill`` but wouldn't know any skill names to pass.
    """
    mgr = ToolManager(
        _make_spec([skill_no_resources]),
    )
    schemas = mgr.get_tool_schemas()
    by_name = {s["function"]["name"]: s for s in schemas}
    assert "load_skill" in by_name
    # The bundled skill ("summarize") is named in the tool description so a
    # loaderless harness can discover and load it.
    assert "summarize" in by_name["load_skill"]["function"]["description"]


def test_schemas_include_read_skill_file_with_resources(
    skill_with_resources: SkillSpec,
) -> None:
    """
    get_tool_schemas includes read_skill_file when a skill
    has bundled resource files.
    """
    mgr = ToolManager(
        _make_spec([skill_with_resources]),
    )
    schemas = mgr.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "read_skill_file" in names


def test_schemas_exclude_read_skill_file_without_resources(
    skill_no_resources: SkillSpec,
) -> None:
    """
    get_tool_schemas does NOT include read_skill_file when
    no skill has bundled resources.
    """
    mgr = ToolManager(
        _make_spec([skill_no_resources]),
    )
    schemas = mgr.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "read_skill_file" not in names


def test_schemas_empty_when_no_skills() -> None:
    """
    get_tool_schemas returns empty when agent has no skills,
    excluding the always-registered lifecycle tool
    (``sys_cancel_task``).
    """
    mgr = ToolManager(_make_spec([]))
    assert _non_lifecycle_schemas(mgr) == []


def test_schemas_isolate_a_failing_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A tool whose ``get_schema`` raises is skipped, not allowed to drop
    the entire toolset: every other tool's schema is still returned and
    a warning names the offending tool.

    Regression test for the silent total-toolset drop in #378. A single
    unimportable ``type: function`` tool (dotted ``callable`` path that
    fails to import) used to abort the whole list comprehension, leaving
    the model with zero declared tools and only a swallowed WARNING.
    """

    class _BoomTool:
        def get_schema(self) -> dict[str, Any]:
            raise ImportError("No module named 'boom'")

    mgr = ToolManager(_make_spec([]))
    healthy = {s["function"]["name"] for s in mgr.get_tool_schemas()}
    assert healthy  # sanity: always-present lifecycle tools exist

    mgr._tools["boom"] = _BoomTool()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="omnigent.tools.manager"):
        schemas = mgr.get_tool_schemas()

    names = {s["function"]["name"] for s in schemas}
    assert "boom" not in names
    assert names == healthy  # every healthy tool survived the bad one
    assert any("boom" in record.getMessage() for record in caplog.records)


def test_client_schemas_isolate_a_failing_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A client-side tool whose ``get_schema`` raises is skipped, not
    allowed to drop the entire client toolset: every other client tool's
    schema is still returned and a warning names the offending tool.

    Mirrors ``test_schemas_isolate_a_failing_tool`` for the
    ``get_client_tool_schemas`` path that ``SpawnTool`` uses to propagate
    client tools to sub-agents (#378).
    """

    class _BoomClientTool(ClientSideTool):
        def get_schema(self) -> dict[str, Any]:
            raise ImportError("No module named 'boom'")

    mgr = ToolManager(_make_spec([]))
    healthy_tool = ClientSideTool(
        ClientSideToolSpec(
            name="weather",
            schema={"type": "function", "function": {"name": "weather"}},
        )
    )
    mgr._tools["weather"] = healthy_tool
    healthy = {s["function"]["name"] for s in mgr.get_client_tool_schemas()}
    assert healthy == {"weather"}  # sanity: the good client tool is advertised

    mgr._tools["boom"] = _BoomClientTool(ClientSideToolSpec(name="boom", schema={}))

    with caplog.at_level(logging.WARNING, logger="omnigent.tools.manager"):
        schemas = mgr.get_client_tool_schemas()

    names = {s["function"]["name"] for s in schemas}
    assert "boom" not in names
    assert names == healthy  # the healthy client tool survived the bad one
    assert any("boom" in record.getMessage() for record in caplog.records)


def test_session_reads_registered_but_writes_gated_without_opt_in() -> None:
    """
    Read-only session discovery (``sys_session_get_history`` /
    ``sys_session_list`` / ``sys_session_get_info``) is registered for
    **every** agent, even one that declares no sub-agents — so a
    user-added agent can read its session-mates for context. The
    mutating session tools (``sys_session_send`` /
    ``sys_session_close`` / ``sys_session_create`` /
    ``sys_session_share``) are NOT registered without an opt-in
    (``tools.agents`` or top-level ``spawn: true``). A regression that
    registered the writes by default would expose the child-session
    spawn surface — and, for share, the ability to expose the session
    to a third party or ``__public__`` — to every custom agent.
    """
    mgr = ToolManager(_make_spec([]))
    names = {s["function"]["name"] for s in mgr.get_tool_schemas()}
    assert "sys_session_get_history" in names
    assert "sys_session_list" in names
    # get_info is a read like peek/list — always available so any agent
    # can inspect a session it can access. If absent, the registration
    # in _register_sub_agent_tools regressed and the orchestrator can't
    # check session status.
    assert "sys_session_get_info" in names
    assert "sys_session_send" not in names
    assert "sys_session_close" not in names
    assert "sys_session_create" not in names
    # Sharing has its OWN dedicated `share:` flag (default `none`), so it
    # is absent here even though this spec also lacks spawn/agents. A
    # regression registering it by default would let any prompt-injected
    # agent expose its session (incl. via __public__).
    assert "sys_session_share" not in names
    # Model awareness pairs with the dispatch grant — without send there
    # is no args.model to pick, so the listing tool must stay gated too.
    assert "sys_list_models" not in names
    # The read-only sys_agent_get/list/download stay always-on.
    assert "sys_agent_get" in names
    assert "sys_agent_list" in names


def test_spawn_flag_registers_write_tools_without_sub_agents() -> None:
    """
    Top-level ``spawn: true`` registers the spawn writes without any
    declared sub-agents — the opt-in for agents that author a config
    locally and launch it via ``sys_session_create(config_path=...)``.
    If this gate arm regressed, ``spawn: true`` in YAML would have no
    runtime effect and the author-and-launch flow would be impossible
    without bundling sub-agent directories.
    """
    spec = AgentSpec(spec_version=1, spawn=True)
    names = {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}
    assert "sys_session_send" in names
    assert "sys_session_close" in names
    assert "sys_session_create" in names
    # The dispatch grant brings model awareness along with it.
    assert "sys_list_models" in names
    # Intelligent routing advisor stays hidden when routing is disabled.
    assert "sys_advise_models" not in names
    # Sharing is DECOUPLED from spawn — its own `share:` flag governs it,
    # so `spawn: true` alone (share defaulting to `none`) does NOT
    # register it. A regression coupling them would re-expose sharing to
    # every spawn-capable agent.
    assert "sys_session_share" not in names


def test_session_send_schema_drops_named_mode_without_sub_agents() -> None:
    """
    With the ``spawn: true`` opt-in but no declared sub-agents,
    ``sys_session_send``'s schema omits the named-mode ``agent`` /
    ``title`` parameters (an empty enum is unusable and invalid for
    some providers) and advertises only the ``session_id`` mode; with
    declared sub-agents, the named mode is present with the
    declared-type enum. If the empty case regressed to emitting
    ``"enum": []``, providers that validate schemas would reject the
    tool list outright.
    """
    bare = ToolManager(AgentSpec(spec_version=1, spawn=True))
    bare_schema = next(
        s for s in bare.get_tool_schemas() if s["function"]["name"] == "sys_session_send"
    )
    bare_params = bare_schema["function"]["parameters"]
    assert set(bare_params["properties"]) == {"session_id", "args"}

    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(agents=["researcher"]),
        sub_agents=[AgentSpec(spec_version=1, name="researcher")],
    )
    named = ToolManager(spec)
    named_schema = next(
        s for s in named.get_tool_schemas() if s["function"]["name"] == "sys_session_send"
    )
    named_params = named_schema["function"]["parameters"]
    assert set(named_params["properties"]) == {"agent", "title", "session_id", "args"}
    # The enum carries exactly the declared sub-agent names.
    assert named_params["properties"]["agent"]["enum"] == ["researcher"]


def test_declared_agents_grant_send_close_but_not_create() -> None:
    """
    Declaring ``tools.agents`` permits spawning ONLY the specified
    sub-agent list: ``sys_session_send`` (named mode over the declared
    enum) and ``sys_session_close`` register, but ``sys_session_create``
    does NOT — launching arbitrary agents (by id or custom bundle)
    requires the separate ``spawn: true`` grant. If create leaked in
    here, a spec that whitelisted two sub-agent types could launch any
    accessible agent or upload arbitrary bundles.
    """
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(agents=["researcher"]),
        sub_agents=[AgentSpec(spec_version=1, name="researcher")],
    )
    mgr = ToolManager(spec)
    names = {s["function"]["name"] for s in mgr.get_tool_schemas()}
    assert "sys_session_send" in names
    assert "sys_session_close" in names
    assert "sys_session_create" not in names
    # Model awareness rides the same grant as send.
    assert "sys_list_models" in names
    # Advisor is capability-gated — absent without a routing client.
    assert "sys_advise_models" not in names
    # Declaring sub-agents does NOT enable sharing — that is the separate
    # `share:` flag's job, decoupled from the spawn/agents grant.
    assert "sys_session_share" not in names


@dataclass
class _FakeRoutingCaps:
    routing_client: object | None = None


def _spawn_spec() -> AgentSpec:
    return AgentSpec(spec_version=1, spawn=True)


def test_advise_models_hidden_when_routing_disabled() -> None:
    """sys_advise_models must not appear when RuntimeCaps.routing_client is None."""
    caps = _FakeRoutingCaps(routing_client=None)
    with patch("omnigent.runtime._globals._caps", new=caps):
        names = {s["function"]["name"] for s in ToolManager(_spawn_spec()).get_tool_schemas()}
    assert "sys_list_models" in names
    assert "sys_advise_models" not in names


def test_advise_models_exposed_when_routing_enabled() -> None:
    """sys_advise_models is advertised alongside send when routing is configured."""
    caps = _FakeRoutingCaps(routing_client=object())
    with patch("omnigent.runtime._globals._caps", new=caps):
        names = {s["function"]["name"] for s in ToolManager(_spawn_spec()).get_tool_schemas()}
    assert "sys_list_models" in names
    assert "sys_advise_models" in names


def test_share_non_public_registers_share_tool_without_public() -> None:
    """
    ``agent_session_sharing: non-public`` alone (no spawn / declared
    agents) registers ``sys_session_share`` — proving the flag is
    independently sufficient AND does not drag in the spawn-lifecycle
    tools. The advertised ``user_id`` schema must NOT mention
    ``__public__``, so the model is not offered a grantee the runner
    would reject. If the gating regressed to the spawn opt-in, share
    would be absent here.
    """
    mgr = ToolManager(AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.NON_PUBLIC))
    schemas = {s["function"]["name"]: s for s in mgr.get_tool_schemas()}
    assert "sys_session_share" in schemas
    # Sharing is decoupled from spawn — none of the spawn writes ride along.
    assert "sys_session_send" not in schemas
    assert "sys_session_close" not in schemas
    assert "sys_session_create" not in schemas
    # non-public must not advertise the public sentinel.
    user_id_desc = schemas["sys_session_share"]["function"]["parameters"]["properties"]["user_id"][
        "description"
    ]
    assert "__public__" not in user_id_desc


def test_share_public_registers_share_tool_advertising_public() -> None:
    """
    ``agent_session_sharing: public`` registers ``sys_session_share``
    and the advertised ``user_id`` schema DOES mention ``__public__`` —
    the only tier where anonymous-read grants are permitted. If
    ``allow_public`` weren't threaded from the flag into the tool, the
    public option would be hidden (or, worse, advertised under
    non-public).
    """
    mgr = ToolManager(AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.PUBLIC))
    schemas = {s["function"]["name"]: s for s in mgr.get_tool_schemas()}
    assert "sys_session_share" in schemas
    user_id_desc = schemas["sys_session_share"]["function"]["parameters"]["properties"]["user_id"][
        "description"
    ]
    assert "__public__" in user_id_desc


def test_both_grants_compose() -> None:
    """
    A spec with BOTH ``tools.agents`` and ``spawn: true`` (the
    nessie/polly shape) gets the union: named-mode send over the
    declared enum, close, AND create for self-defined children. If
    composition regressed (e.g. one arm short-circuiting the other),
    an orchestrator would lose either its declared sub-agents or its
    author-and-launch flow.
    """
    spec = AgentSpec(
        spec_version=1,
        spawn=True,
        tools=ToolsConfig(agents=["researcher"]),
        sub_agents=[AgentSpec(spec_version=1, name="researcher")],
    )
    schemas = {s["function"]["name"]: s["function"] for s in ToolManager(spec).get_tool_schemas()}
    assert "sys_session_close" in schemas
    assert "sys_session_create" in schemas
    # Named mode survives alongside the spawn grant.
    assert schemas["sys_session_send"]["parameters"]["properties"]["agent"]["enum"] == [
        "researcher"
    ]


def test_session_get_info_schema_has_optional_session_id() -> None:
    """
    ``sys_session_get_info`` advertises a single optional ``session_id``
    parameter (omitting it defaults to the caller's own session). If
    ``session_id`` became required, an agent inspecting its own session
    would be forced to look up its own id first.
    """
    mgr = ToolManager(_make_spec([]))
    schema = next(
        s for s in mgr.get_tool_schemas() if s["function"]["name"] == "sys_session_get_info"
    )
    params = schema["function"]["parameters"]
    assert set(params["properties"]) == {"session_id"}
    # No required fields — session_id is optional by design.
    assert params["required"] == []


def test_agent_read_tools_registered_for_every_agent() -> None:
    """
    ``sys_agent_get`` and ``sys_agent_download`` are registered for
    **every** agent — they are global, permission-bounded reads of any
    accessible session's agent/bundle, like the session reads. If they
    regress out of registration, an orchestrator can't inspect or fork
    agents it can see.
    """
    mgr = ToolManager(_make_spec([]))
    names = {s["function"]["name"] for s in mgr.get_tool_schemas()}
    assert "sys_agent_get" in names
    assert "sys_agent_download" in names
    assert "sys_agent_list" in names
    # get/download require a session_id (an agent is only inspectable
    # while running in some session); list takes no parameters.
    for tool_name in ("sys_agent_get", "sys_agent_download"):
        schema = next(s for s in mgr.get_tool_schemas() if s["function"]["name"] == tool_name)
        assert "session_id" in schema["function"]["parameters"]["required"]
    list_schema = next(
        s for s in mgr.get_tool_schemas() if s["function"]["name"] == "sys_agent_list"
    )
    assert list_schema["function"]["parameters"]["properties"] == {}


# ── MCP integration ──────────────────────────────────────


def test_shutdown_safe_without_start() -> None:
    """
    ``shutdown()`` is safe to call without ``start()``.
    """
    spec = _make_spec()
    mgr = ToolManager(spec)
    mgr.shutdown()


def test_shutdown_idempotent() -> None:
    """Calling ``shutdown()`` twice does not raise."""
    spec = _make_spec()
    mgr = ToolManager(spec)
    mgr.start()
    mgr.shutdown()
    mgr.shutdown()


def test_shutdown_closes_os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``shutdown()`` closes ``_os_env`` when it was self-created."""
    spec = _make_spec()
    mgr = ToolManager(spec)
    mgr.start()

    class _FakeOSEnv:
        closed = False

        def close(self) -> None:
            self.closed = True

    fake_env = _FakeOSEnv()
    mgr._os_env = fake_env  # type: ignore[assignment]
    mgr._pre_resolved_os_env = None

    mgr.shutdown()
    assert fake_env.closed
    assert mgr._os_env is None


def test_shutdown_skips_pre_resolved_os_env() -> None:
    """``shutdown()`` does NOT close a pre-resolved (shared) OS env."""
    spec = _make_spec()

    class _FakeOSEnv:
        closed = False

        def close(self) -> None:
            self.closed = True

    shared_env = _FakeOSEnv()
    mgr = ToolManager(spec, os_env=shared_env)  # type: ignore[arg-type]
    mgr.start()
    mgr.shutdown()
    assert not shared_env.closed


def test_shutdown_calls_tool_shutdown() -> None:
    """``shutdown()`` calls ``shutdown()`` on every registered tool."""
    from omnigent.tools.base import Tool

    class _TrackingTool(Tool):
        shut_down = False

        @classmethod
        def name(cls) -> str:
            return "_tracking"

        @classmethod
        def description(cls) -> str:
            return "test"

        def get_schema(self) -> dict[str, Any]:
            return {"type": "function", "function": {"name": "_tracking"}}

        def shutdown(self) -> None:
            self.shut_down = True

    spec = _make_spec()
    mgr = ToolManager(spec)
    tracker = _TrackingTool()
    mgr._tools["_tracking"] = tracker
    mgr.start()
    mgr.shutdown()
    assert tracker.shut_down


# ── Client-specified tools ────────────────────────────────


def _make_client_side_spec(name: str) -> ClientSideToolSpec:
    """
    Build a minimal :class:`ClientSideToolSpec` for use in manager tests.

    :param name: Tool function name, e.g. ``"get_weather"``.
    :returns: A :class:`ClientSideToolSpec` with a minimal schema.
    """
    return ClientSideToolSpec(
        name=name,
        schema={
            "type": "function",
            "function": {"name": name, "description": "A test tool.", "parameters": {}},
        },
    )


def test_client_tools_registered_in_schemas() -> None:
    """
    Client-specified tools appear in get_tool_schemas() alongside
    built-in tools without calling start().

    A failure here means the LLM never sees client tools — the
    client_tool_specs constructor arg is not being wired up.
    """
    spec = _make_spec()
    mgr = ToolManager(
        spec,
        client_tool_specs=[
            _make_client_side_spec("get_weather"),
            _make_client_side_spec("send_email"),
        ],
    )

    # Filter out the always-present lifecycle tool
    # (sys_cancel_task) — orthogonal to the client-tool
    # registration being tested.
    schemas = _non_lifecycle_schemas(mgr)
    names = [s["function"]["name"] for s in schemas]

    # Both client tools appear in schemas — 2 registered, 2 returned
    assert len(schemas) == 2, (
        f"Expected 2 schemas (2 client tools), got {len(schemas)}. "
        "If 0, client_tool_specs are not being registered."
    )
    assert "get_weather" in names
    assert "send_email" in names


def test_is_client_side_tool_returns_true_for_registered_client_tools() -> None:
    """
    is_client_side_tool returns True for registered ClientSideTool
    entries and False for built-in tools and unknown names.

    The agent loop uses this to detect when to complete the response
    instead of executing tools server-side. A failure here would
    cause client-side tools to be dispatched through call_tool,
    triggering RuntimeError from ClientSideTool.invoke.
    """
    spec = _make_spec(skills=[SkillSpec(name="summarize", description=".", content=".")])
    mgr = ToolManager(
        spec,
        client_tool_specs=[
            _make_client_side_spec("get_weather"),
            _make_client_side_spec("send_email"),
        ],
    )

    # Client tools are detected as client-side
    assert mgr.is_client_side_tool("get_weather") is True, (
        "Expected True for registered ClientSideTool 'get_weather'. "
        "If False, is_client_side_tool is not checking isinstance(tool, ClientSideTool)."
    )
    assert mgr.is_client_side_tool("send_email") is True

    # Built-in tool is not client-side
    assert mgr.is_client_side_tool("load_skill") is False, (
        "Expected False for built-in 'load_skill'. "
        "If True, is_client_side_tool is not type-checking correctly."
    )

    # Unregistered tool is not client-side
    assert mgr.is_client_side_tool("nonexistent") is False


def test_client_tool_shadows_skill_tool(
    skill_no_resources: SkillSpec,
) -> None:
    """
    A client tool with the same name as a skill tool overwrites the
    skill tool (last registered wins, with a warning).

    This ensures the override behavior is intentional — clients can
    replace spec-defined tools at request time.
    """
    spec = _make_spec(skills=[skill_no_resources])
    mgr = ToolManager(
        spec,
        # 'load_skill' is the built-in skill tool name
        client_tool_specs=[_make_client_side_spec("load_skill")],
    )

    schemas = mgr.get_tool_schemas()
    # Only one 'load_skill' — client version overwrote built-in
    names = [s["function"]["name"] for s in schemas]
    assert names.count("load_skill") == 1, (
        f"Expected exactly one 'load_skill' (client overwrite), got {names.count('load_skill')}."
    )

    # The registered tool is the client's ClientSideTool, not LoadSkillTool
    from omnigent.tools.client_specified import ClientSideTool

    assert isinstance(mgr._tools["load_skill"], ClientSideTool), (
        "Expected ClientSideTool after client override, "
        f"got {type(mgr._tools['load_skill']).__name__}."
    )


def test_client_tools_none_equivalent_to_empty() -> None:
    """
    Passing client_tool_specs=None and client_tool_specs=[] produce
    the same result: no client tools registered.
    """
    spec = _make_spec()
    mgr_none = ToolManager(spec, client_tool_specs=None)
    mgr_empty = ToolManager(spec, client_tool_specs=[])

    assert _non_lifecycle_schemas(mgr_none) == []
    assert _non_lifecycle_schemas(mgr_empty) == []


# ── Spec-declared client tools (runtime: client) ──────────


def _spec_with_local(
    name: str,
    runtime: ToolRuntime = ToolRuntime.SERVER,
    parameters: dict[str, Any] | None = None,
) -> AgentSpec:
    """
    Build an :class:`AgentSpec` with a single local-tool entry.

    Mirrors the per-entry ``runtime`` field that
    :class:`LocalToolInfo` (and only :class:`LocalToolInfo` —
    builtins are intrinsically server-side) carries. Server-runtime
    entries declare a fake ``path``/``language`` so the existing
    loaders skip them gracefully when ``workdir`` is ``None``;
    client-runtime entries declare ``parameters`` (required by the
    validator and by the manager's :class:`ClientSideTool`
    registration).

    :param name: The local tool name, e.g. ``"open_in_editor"``.
    :param runtime: Either :attr:`ToolRuntime.SERVER` (default) or
        :attr:`ToolRuntime.CLIENT`. ``CLIENT`` opts the tool into
        the spec-declared client-side dispatch path under test.
    :param parameters: JSON-Schema ``parameters`` block. Required
        for ``CLIENT`` entries (no callable to introspect); ignored
        for ``SERVER`` entries.
    :returns: A minimal :class:`AgentSpec` with that single local
        tool.
    """
    if runtime == ToolRuntime.CLIENT:
        info = LocalToolInfo(
            name=name,
            path=None,
            language="python",
            runtime=runtime,
            parameters=parameters or {"type": "object", "properties": {}},
        )
    else:
        info = LocalToolInfo(
            name=name,
            path=f"tools/python/{name}.py",
            language="python",
            runtime=runtime,
        )
    return AgentSpec(
        spec_version=1,
        local_tools=[info],
    )


def test_is_client_side_tool_true_for_spec_declared_client_local_tool() -> None:
    """
    A local tool declared with ``runtime: client`` in the spec is
    reported as client-side by :meth:`ToolManager.is_client_side_tool`,
    even when no request-time ``client_tool_specs`` are passed.

    Claim: spec-declared client tools take the same dispatch path
    (``action_required``) as request-supplied client tools.
    Failure here would mean ``runtime: client`` is silently ignored
    and the workflow would try to execute the local tool server-side.
    """
    spec = _spec_with_local("open_in_editor", runtime=ToolRuntime.CLIENT)
    mgr = ToolManager(spec)

    # Spec-declared client tool is detected as client-side.
    assert mgr.is_client_side_tool("open_in_editor") is True, (
        "Expected True for 'open_in_editor' declared as runtime: client. "
        "If False, ToolManager is not consulting the spec's runtime field."
    )


def test_is_client_side_tool_false_for_default_server_local_tool() -> None:
    """
    A local tool without ``runtime: client`` (default
    :attr:`ToolRuntime.SERVER`) is NOT reported as client-side.

    Claim: the default runtime ``SERVER`` preserves the existing
    server-side dispatch path. Failure would mean every local tool
    is treated as client-side regardless of its declared runtime —
    catastrophic regression of the existing path.

    Note: the manager is created with ``workdir=None`` so the
    underlying file-based loader doesn't try to open the
    nonexistent path on disk. Only the ``runtime`` field matters
    for what we're asserting here.
    """
    spec = _spec_with_local("echo_tool", runtime=ToolRuntime.SERVER)
    mgr = ToolManager(spec)

    assert mgr.is_client_side_tool("echo_tool") is False, (
        "Expected False for 'echo_tool' with default runtime: server. "
        "If True, the default runtime is being misread."
    )


def test_spec_client_local_tool_schema_still_visible_to_llm() -> None:
    """
    The schema for a spec-declared client tool is still emitted by
    :meth:`ToolManager.get_tool_schemas`, so the LLM sees the tool
    and can call it.

    Claim: marking a tool ``runtime: client`` does not hide its
    schema from the LLM — it only changes the dispatch path.
    Failure would mean the LLM never learns the tool exists and
    can't invoke it, defeating the feature.
    """
    spec = _spec_with_local(
        "open_in_editor",
        runtime=ToolRuntime.CLIENT,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    mgr = ToolManager(spec)

    schemas = mgr.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "open_in_editor" in names, (
        f"Expected 'open_in_editor' in tool schemas even when runtime=client. "
        f"Got {names}. If missing, the spec-declared client local tool "
        f"is not being registered as a ClientSideTool."
    )
    # The parameters block from the spec must be carried through to
    # the LLM-facing schema; otherwise the LLM emits empty arguments.
    fn = next(s["function"] for s in schemas if s["function"]["name"] == "open_in_editor")
    assert fn["parameters"]["required"] == ["path"], (
        f"Expected parameters.required=['path'] from the spec to flow "
        f"through to the schema, got {fn.get('parameters')!r}."
    )


def test_spec_client_local_tool_in_get_client_tool_schemas() -> None:
    """
    :meth:`ToolManager.get_client_tool_schemas` includes spec-declared
    client tools alongside request-time :class:`ClientSideTool`
    instances.

    Claim: sub-agents launched via SpawnTool inherit spec-declared
    client tools the same way they inherit request-supplied ones —
    both go through ``get_client_tool_schemas``. Failure would mean
    sub-agents lose the spec-declared client tool surface.
    """
    spec = _spec_with_local("open_in_editor", runtime=ToolRuntime.CLIENT)
    mgr = ToolManager(
        spec,
        client_tool_specs=[_make_client_side_spec("get_weather")],
    )

    names = [s["function"]["name"] for s in mgr.get_client_tool_schemas()]

    # Both paths represented: 1 spec-declared + 1 request-supplied.
    assert sorted(names) == ["get_weather", "open_in_editor"], (
        f"Expected both client paths in get_client_tool_schemas, "
        f"got {sorted(names)}. If 'open_in_editor' is missing, "
        f"spec-declared client tools are not propagated to sub-agents. "
        f"If 'get_weather' is missing, the request-time path regressed."
    )


def test_spec_client_and_request_client_coexist() -> None:
    """
    Spec-declared and request-supplied client tools both register
    and both are reported as client-side.

    Claim: the two paths are additive — a single ToolManager can
    expose tools from both sources, and ``is_client_side_tool``
    returns True for both. Failure would mean one path overrides
    or hides the other, breaking the integration story.
    """
    spec = _spec_with_local("open_in_editor", runtime=ToolRuntime.CLIENT)
    mgr = ToolManager(
        spec,
        client_tool_specs=[_make_client_side_spec("get_weather")],
    )

    # Spec-declared path.
    assert mgr.is_client_side_tool("open_in_editor") is True
    # Request-time path (existing behavior preserved).
    assert mgr.is_client_side_tool("get_weather") is True

    schema_names = [s["function"]["name"] for s in mgr.get_tool_schemas()]
    assert "open_in_editor" in schema_names
    assert "get_weather" in schema_names


def test_is_client_side_tool_false_for_unregistered_name() -> None:
    """
    :meth:`ToolManager.is_client_side_tool` returns False for an
    unknown name even after introducing the spec-declared path.

    Guards against an off-by-one where a name typo'd in the spec
    (or arbitrary string) gets misclassified as client-side just
    because it's in some set.
    """
    spec = _spec_with_local("open_in_editor", runtime=ToolRuntime.CLIENT)
    mgr = ToolManager(spec)

    assert mgr.is_client_side_tool("definitely_not_a_real_tool") is False


@pytest.mark.parametrize(
    "name",
    [
        "tool with spaces",
        "tool:colon",
        "",
        "a" * 257,
    ],
    ids=[
        "spaces",
        "colon",
        "empty",
        "too_long",
    ],
)
def test_client_tool_invalid_name_raises(
    name: str,
) -> None:
    """
    Client-specified tools with invalid names raise
    ``OmnigentError`` at registration time.
    """
    spec = _make_spec()
    with pytest.raises(OmnigentError, match="Invalid client tool name"):
        ToolManager(
            spec,
            client_tool_specs=[_make_client_side_spec(name)],
        )


# ── Local tool registration ──────────────────────────


def _write_local_tool(
    workdir: Path,
    filename: str,
    schema_name: str,
) -> None:
    """
    Write a minimal local Python tool file to
    ``workdir/tools/python/<filename>``.

    :param workdir: Agent image root directory.
    :param filename: File name, e.g. ``"web_fetch.py"``.
    :param schema_name: The ``@tool``-decorated function's name —
        becomes the LLM-facing tool name.
    """
    py_dir = workdir / "tools" / "python"
    py_dir.mkdir(parents=True, exist_ok=True)
    code = (
        '"""Test tool."""\n'
        "from omnigent_client import tool\n"
        "\n"
        "\n"
        "@tool\n"
        f"def {schema_name}() -> str:\n"
        '    """Execute."""\n'
        '    return "local_tool_result"\n'
    )
    (py_dir / filename).write_text(code)


def test_local_tools_registered_and_callable(
    tmp_path: Path,
) -> None:
    """
    ToolManager registers local Python tools from the workdir
    and dispatches calls to them.
    """
    _write_local_tool(tmp_path, "echo_tool.py", "echo_tool")
    info = LocalToolInfo(
        name="echo_tool",
        path="tools/python/echo_tool.py",
        language="python",
    )
    spec = _make_spec(local_tools=[info])
    mgr = ToolManager(spec, workdir=tmp_path)
    schemas = mgr.get_tool_schemas()
    # Local tool appears in the schema list.
    names = [s["function"]["name"] for s in schemas]
    assert "echo_tool" in names, (
        f"Expected 'echo_tool' in schemas, got {names}. "
        f"If missing, _register_local_tools did not register the tool."
    )
    # Dispatching works through call_tool.
    result = mgr.call_tool("echo_tool", json.dumps({}), _TEST_CTX)
    assert result == "local_tool_result", (
        f"Expected 'local_tool_result', got {result!r}. "
        f"If 'Error: tool not found', the tool was not registered."
    )


def test_local_tools_skipped_without_workdir() -> None:
    """
    ToolManager with workdir=None skips local tool registration
    without error, even if spec has local_tools.
    """
    info = LocalToolInfo(
        name="some_tool",
        path="tools/python/some_tool.py",
        language="python",
    )
    spec = _make_spec(local_tools=[info])
    # workdir=None (default) — should not raise.
    mgr = ToolManager(spec)
    # Excluding the always-present lifecycle tool
    # (sys_cancel_task), no tools should register when workdir
    # is None and no skills / builtins are set.
    assert _non_lifecycle_schemas(mgr) == [], (
        "No tools (apart from the always-present sys_cancel_task) "
        "should be registered when workdir is None."
    )


# ── web_search builtin: Databricks model does not emit web_search_preview ───


def test_web_search_does_not_emit_web_search_preview_for_databricks_model() -> None:
    """
    When the agent's model is a ``databricks-*`` model, the ``web_search``
    builtin must NOT emit ``{"type": "web_search_preview"}`` in its schema.
    Databricks rejects that tool type at the API level with HTTP 400, killing
    the request before the agent runs.

    A failure here means the ``databricks-*`` guard in
    ``ToolManager._create_web_search`` was removed or the prefix changed.
    The wrong schema would be ``{"type": "web_search_preview"}``.
    """
    spec = AgentSpec(
        spec_version=1,
        llm=LLMConfig(model="databricks-gpt-5-4"),
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name="web_search")]),
    )
    mgr = ToolManager(spec)
    tool = mgr.get_tool("web_search")

    assert tool is not None, "web_search should be registered when declared in tools.builtins"
    schema = tool.get_schema()
    assert schema.get("type") != "web_search_preview", (
        f'web_search emitted {{"type": "web_search_preview"}} for '
        f"databricks-gpt-5-4 — Databricks does not support this tool type "
        f"and rejects the request with HTTP 400. Got schema: {schema!r}"
    )
