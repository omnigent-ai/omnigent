"""Unit tests for ``omnigent.spec.omnigent.agent_spec_to_agent_def``.

Phase 1 ships the forward direction only. These tests hand-craft
:class:`AgentSpec` objects and assert the resulting
:class:`omnigent.datamodel.AgentDef` has the expected shape. The
round-trip test (``agent_spec_to_agent_def(agent_def_to_agent_spec(d)) == d``)
lands in phase 2 once the reverse direction exists.

Fail-loud tests cover every phase-2-or-later concept the translator
explicitly rejects: policies, sandbox, MCP servers, and cancellable-
function tools. Each test asserts the error message names the
specific spec field so the caller can act on it.
"""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.tools import AgentTool, FunctionTool
from omnigent.spec import (
    AgentSpec,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    LLMConfig,
    LocalToolInfo,
    MCPServerConfig,
    Phase,
    PhaseSelector,
    PolicyAction,
    ToolRuntime,
    ToolsConfig,
)
from omnigent.spec.omnigent import (
    agent_def_to_agent_spec,
    agent_spec_to_agent_def,
)

# SandboxConfig is not re-exported from omnigent.spec's public
# __init__ because it is only addressable under the ``tools.sandbox``
# sub-block; the translator test needs it directly to construct a
# sandboxed ToolsConfig.
from omnigent.spec.types import SandboxConfig


# A sample callable used as the target of dotted-path tool resolution.
# Defined at module scope so the importlib-based resolver can find it
# via ``tests.spec.test_omnigent_translator.sample_tool_callable``.
def sample_tool_callable(query: str) -> str:
    """
    Stub tool used only as a dotted-path target in the translator tests.

    :param query: Arbitrary string; value is not inspected.
    :returns: The string ``"ok"``.
    """
    del query
    return "ok"


NOT_A_FUNCTION = "this is a string, not a function"
"""Module-level non-callable attribute used to verify fail-loud
behavior when the dotted path resolves to a non-callable object."""


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def basic_spec() -> AgentSpec:
    """
    Minimal ``AgentSpec`` targeting the omnigent executor.

    :returns: A spec with name, instructions, one ``llm.model``, and
        ``executor.type == "omnigent"`` carrying harness+profile.
    """
    return AgentSpec(
        spec_version=1,
        name="hello-agent",
        instructions="You are a helpful assistant.",
        llm=LLMConfig(model="databricks-claude-sonnet-4-6"),
        executor=ExecutorSpec(
            type="omnigent",
            model="databricks-claude-sonnet-4-6",
            config={
                "harness": "claude-sdk",
                "profile": "test-profile",
            },
        ),
    )


# ── Happy-path translation ──────────────────────────────────────────


def test_basic_spec_produces_agent_def_with_name_and_prompt(
    basic_spec: AgentSpec,
) -> None:
    """
    The translator copies ``name`` and ``instructions`` into
    ``AgentDef.name`` and ``AgentDef.prompt``.

    **What breaks if this fails**: the omnigent harness can
    start with an unnamed/unprompted agent, silently degrading
    to whatever default the harness falls back to — the exact
    behavior the "fail loud on missing data" principle tries to
    prevent.
    """
    agent_def = agent_spec_to_agent_def(basic_spec)
    # Exact content assertions — a fuzzy "startswith" would pass even
    # if the translator truncated the instructions silently.
    assert agent_def.name == "hello-agent"
    assert agent_def.prompt == "You are a helpful assistant."


def test_basic_spec_maps_llm_and_executor_config(
    basic_spec: AgentSpec,
) -> None:
    """
    ``llm.model``, ``executor.config.harness``, and
    ``executor.config.profile`` populate ``AgentDef.executor``.

    **What breaks if this fails**: the omnigent
    ``create_executor`` factory selects the wrong harness (or
    falls back to a MockExecutor), and the agent runs with the
    wrong backend.
    """
    agent_def = agent_spec_to_agent_def(basic_spec)
    assert agent_def.executor is not None
    assert agent_def.executor.model == "databricks-claude-sonnet-4-6"
    assert agent_def.executor.harness == "claude-sdk"
    assert agent_def.executor.profile == "test-profile"


def test_missing_profile_maps_to_none(
    basic_spec: AgentSpec,
) -> None:
    """
    ``executor.config`` may omit ``profile``; the translator
    surfaces ``None`` so the omnigent
    :class:`~omnigent.datamodel.ExecutorSpec.profile` field
    reflects absence faithfully rather than coercing to a
    sentinel string.

    **What breaks if this fails**: a translator regression
    that re-introduces ``""`` as a stand-in for "absent" — the
    kind of empty-string-sentinel antipattern upstream's
    empty-string-graduation branch just finished removing.
    """
    basic_spec.executor.config.pop("profile")
    agent_def = agent_spec_to_agent_def(basic_spec)
    assert agent_def.executor is not None
    assert agent_def.executor.profile is None


def test_spec_with_local_tool_resolves_dotted_path(
    basic_spec: AgentSpec,
) -> None:
    """
    A ``LocalToolInfo`` with a dotted import path is resolved via
    :func:`importlib.import_module` and wrapped in a
    :class:`FunctionTool` whose ``callable`` is the real Python
    function.

    **What breaks if this fails**: the harness receives a tool
    with no callable, and every invocation of the tool raises
    from inside the harness rather than from the translator —
    much harder to diagnose.
    """
    basic_spec.local_tools = [
        LocalToolInfo(
            name="sample",
            path="tests.spec.test_omnigent_translator.sample_tool_callable",
            language="python",
        ),
    ]
    agent_def = agent_spec_to_agent_def(basic_spec)
    assert "sample" in agent_def.tools
    tool = agent_def.tools["sample"]
    assert isinstance(tool, FunctionTool)
    # Asserting the exact callable identity proves the resolver
    # pulled from the right module, not a namesake defined elsewhere.
    assert tool.callable is sample_tool_callable


# ── Fail-loud rejection of phase-2 concepts ─────────────────────────


def test_policies_dropped_from_forward_translation(
    basic_spec: AgentSpec,
) -> None:
    """
    A spec with ``guardrails.policies`` translates successfully
    to an :class:`AgentDef` and the resulting def carries NO
    policy metadata — the harness is agnostic to policies
    because omnigent enforces them upstream of the executor.

    **What breaks if this fails**: two regressions to guard
    against:
    1. The translator starts rejecting again (``OmnigentError``
       with ``"policies"``) — the omnigent executor would then
       be unusable for any spec carrying a ``guardrails:`` block,
       which the whole policy-lift pipeline just enabled.
    2. The translator starts round-tripping policies INTO the
       AgentDef — meaning both the omnigent workflow AND the
       omnigent runtime would enforce them, double-counting
       every DENY.
    """
    basic_spec.guardrails = GuardrailsSpec(
        policies=[
            FunctionPolicySpec(
                name="noop",
                on=[PhaseSelector(Phase.TOOL_CALL)],
                function=FunctionRef(
                    path="tests.spec.test_omnigent_translator.sample_tool_callable"
                ),
                action=[PolicyAction.ALLOW],
            ),
        ],
    )
    agent_def = agent_spec_to_agent_def(basic_spec)
    # No rejection. And no policy metadata carried to the
    # harness — the AgentDef's ``policies`` registry is empty
    # because policies are upstream of the executor.
    assert agent_def.policies == {}


def test_sandbox_block_rejected_with_clear_message(
    basic_spec: AgentSpec,
) -> None:
    """
    A spec that requests a sandbox (``tools.sandbox.container_image``)
    is rejected with a message naming ``sandbox``.

    **What breaks if this fails**: the harness runs tools outside
    the sandbox the spec asked for — a security violation.
    """
    basic_spec.tools = ToolsConfig(
        sandbox=SandboxConfig(container_image="python:3.12-slim"),
    )
    with pytest.raises(OmnigentError, match=r"sandbox"):
        agent_spec_to_agent_def(basic_spec)


def test_mcp_server_rejected_with_clear_message(
    basic_spec: AgentSpec,
) -> None:
    """
    A spec that declares an MCP server translates into an
    omnigent MCP tool.

    **What breaks if this fails**: MCP tools disappear from the
    translated agent, so the LLM loses advertised capabilities.
    """
    basic_spec.mcp_servers = [
        MCPServerConfig(name="github", url="https://mcp.example.com/sse"),
    ]
    agent_def = agent_spec_to_agent_def(basic_spec)
    assert "github" in agent_def.tools
    assert getattr(agent_def.tools["github"], "url", None) == "https://mcp.example.com/sse"


def test_tool_with_filesystem_path_rejected(
    basic_spec: AgentSpec,
) -> None:
    """
    A ``LocalToolInfo`` whose path looks like a filesystem path
    (contains ``/`` or ends in ``.py``) is rejected with a
    message naming the dotted-path requirement.

    **What breaks if this fails**: the translator silently drops
    the tool or raises an opaque ``ModuleNotFoundError`` from
    deep inside ``importlib`` — both are worse than a clear
    "use a dotted path" message.
    """
    basic_spec.local_tools = [
        LocalToolInfo(
            name="bad",
            path="tools/python/foo.py",
            language="python",
        ),
    ]
    with pytest.raises(OmnigentError, match=r"dotted"):
        agent_spec_to_agent_def(basic_spec)


def test_tool_with_unimportable_module_rejected(
    basic_spec: AgentSpec,
) -> None:
    """
    A dotted path whose module cannot be imported yields a
    clear error naming the module.

    **What breaks if this fails**: a typo in the tool path
    surfaces as a misleading stack trace instead of a
    spec-validation error.
    """
    basic_spec.local_tools = [
        LocalToolInfo(
            name="missing",
            path="no_such_module_xyzzy.some_function",
            language="python",
        ),
    ]
    with pytest.raises(OmnigentError, match=r"no_such_module_xyzzy"):
        agent_spec_to_agent_def(basic_spec)


def test_tool_pointing_at_non_callable_rejected(
    basic_spec: AgentSpec,
) -> None:
    """
    A dotted path that resolves to a non-callable attribute is
    rejected with a clear error.

    Step (c) made plain callables the only supported tool
    shape on the Omnigent side; runner-protocol instances no longer
    have a fallback. The translator fails loud rather than
    wrapping a non-callable in a tool the harness can't invoke.

    **What breaks if this fails**: the omnigent harness
    registers a FunctionTool whose ``callable`` is a string,
    dict, or other non-callable — every invocation fails with
    ``TypeError`` inside the harness.
    """
    basic_spec.local_tools = [
        LocalToolInfo(
            name="stringy",
            path="tests.spec.test_omnigent_translator.NOT_A_FUNCTION",
            language="python",
        ),
    ]
    # Error names the violation specifically (``non-callable``
    # / runner-protocol retirement note) so the YAML author
    # knows what's expected.
    with pytest.raises(
        OmnigentError,
        match=r"non-callable",
    ):
        agent_spec_to_agent_def(basic_spec)


def test_missing_llm_rejected(basic_spec: AgentSpec) -> None:
    """
    A spec with ``executor.type='omnigent'`` but no ``llm``
    block is rejected — the omnigent harness needs a model
    name. We fail loud at translation time, not deep inside the
    harness constructor.
    """
    basic_spec.executor.model = None
    with pytest.raises(OmnigentError, match=r"executor\.model"):
        agent_spec_to_agent_def(basic_spec)


# ── Harness inference for native Omnigent v1 specs ────────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected_harness"),
    [
        # Claude models must get claude-sdk; before the fix they fell
        # back to DatabricksExecutor and the agent hung.
        ("databricks-claude-sonnet-4", "claude-sdk"),
        ("databricks-claude-sonnet-4-6", "claude-sdk"),
        # GPT models should get openai-agents.
        ("databricks-gpt-5-4", "openai-agents"),
    ],
)
def test_native_omnigent_spec_infers_harness_from_model(
    model: str,
    expected_harness: str,
) -> None:
    """
    Native Omnigent v1 specs use ``executor.type="omnigent"`` with no harness in
    ``executor.config``.  :func:`agent_spec_to_agent_def` must infer
    the harness from the model prefix so Claude models don't fall back
    to ``DatabricksExecutor``.

    A failure means the harness inference call was removed or the model
    prefix table was updated without propagating to the translator.
    """
    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        instructions="You are helpful.",
        llm=LLMConfig(model=model),
        executor=ExecutorSpec(type="omnigent", model=model, config={}),
    )
    agent_def = agent_spec_to_agent_def(spec)
    assert agent_def.executor is not None
    # Wrong harness → wrong executor class at runtime; Claude with
    # DatabricksExecutor sends to Responses API which rejects it.
    assert agent_def.executor.harness == expected_harness, (
        f"Model {model!r}: expected harness {expected_harness!r}, "
        f"got {agent_def.executor.harness!r}."
    )


def test_sub_agent_infers_harness_and_forwards_os_env() -> None:
    """
    When a parent spec's sub-agent uses a native Omnigent v1 executor (no
    harness in ``executor.config``), :func:`agent_spec_to_agent_def`
    must infer the harness from the sub-agent's model prefix AND forward
    ``os_env`` to the returned :class:`AgentTool`.

    A failure means either:
    - Harness inference was dropped from ``_sub_spec_to_agent_tool``
      (sub-agent gets wrong executor).
    - ``os_env`` was not forwarded (sub-session boots without filesystem
      tools even though the spec declares ``os_env: caller_process``).
    """
    sub_os_env = OSEnvSpec(type="caller_process", cwd=".")
    sub_spec = AgentSpec(
        spec_version=1,
        name="backend_engineer",
        instructions="You write code.",
        llm=LLMConfig(model="databricks-claude-sonnet-4"),
        executor=ExecutorSpec(type="omnigent", model="databricks-claude-sonnet-4", config={}),
        os_env=sub_os_env,
    )
    parent_spec = AgentSpec(
        spec_version=1,
        name="root",
        instructions="You delegate.",
        llm=LLMConfig(model="databricks-gpt-5-4"),
        executor=ExecutorSpec(type="omnigent", model="databricks-gpt-5-4", config={}),
        tools=ToolsConfig(agents=["backend_engineer"]),
        sub_agents=[sub_spec],
    )

    agent_def = agent_spec_to_agent_def(parent_spec)
    sub_tool = agent_def.tools.get("backend_engineer")

    assert isinstance(sub_tool, AgentTool), (
        "backend_engineer should be an AgentTool in the parent's tool registry."
    )
    # Wrong harness → sub-agent hits Responses API passthrough which
    # rejects databricks-claude-* with HTTP 400.
    assert sub_tool.executor is not None
    assert sub_tool.executor.harness == "claude-sdk", (
        f"Expected harness 'claude-sdk', got {sub_tool.executor.harness!r}. "
        "Harness inference in _sub_spec_to_agent_tool may be missing."
    )
    # Missing os_env → sub-session boots without file read/write/shell tools.
    assert sub_tool.os_env == sub_os_env, (
        f"os_env not forwarded: expected {sub_os_env!r}, got {sub_tool.os_env!r}. "
        "os_env=sub.os_env may have been dropped from the AgentTool constructor."
    )


# ── Client-runtime tool translation (forward + reverse) ────────────


def test_client_runtime_tool_translates_with_no_callable(
    basic_spec: AgentSpec,
) -> None:
    """
    A ``LocalToolInfo`` with ``runtime=ToolRuntime.CLIENT`` and
    ``path=None`` translates to an ``AgentDef`` ``FunctionTool``
    whose ``runtime == "client"`` and ``callable is None``. The
    translator must NOT attempt to import a dotted path (there
    is none) and must NOT fall through to the
    ``non-callable`` rejection branch.

    What breaks if this fails: client-runtime YAMLs either
    ``ImportError`` deep inside ``importlib`` (translator tries
    to resolve ``None``) or arrive at the harness without the
    discriminator, so the runtime can't tell them apart from
    server tools.
    """
    basic_spec.local_tools = [
        LocalToolInfo(
            name="open_in_editor",
            path=None,
            language="python",
            runtime=ToolRuntime.CLIENT,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    ]
    agent_def = agent_spec_to_agent_def(basic_spec)
    assert "open_in_editor" in agent_def.tools
    tool = agent_def.tools["open_in_editor"]
    assert isinstance(tool, FunctionTool)
    # The discriminator survived translation. If this flips to
    # ``"server"``, the runtime path that emits ``action_required``
    # for spec-declared client tools won't fire.
    assert tool.runtime == "client"
    # No server-side callable for client-runtime tools — the SDK
    # consumer implements them at stream-start time.
    assert tool.callable is None
    # Parameters round-tripped — the LLM relies on this schema
    # to construct calls; losing it would silently degrade tool
    # calling to "no arguments".
    assert tool.input_schema == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }


def test_server_runtime_tool_with_no_path_rejected(
    basic_spec: AgentSpec,
) -> None:
    """
    A ``LocalToolInfo`` declared with ``runtime=ToolRuntime.SERVER``
    but ``path=None`` is rejected at translation time.

    The validator catches this for every spec routed through
    :func:`omnigent.spec.validator.validate`, but the translator
    is also reachable from callers that bypass validation (e.g.
    direct in-memory construction in tests/tools). Failing loud
    here keeps the contract honest end-to-end.

    What breaks if this fails: a malformed spec silently produces
    a ``FunctionTool`` with ``callable=None`` and ``runtime="server"``,
    which the harness then attempts to invoke and crashes with a
    much less useful ``TypeError``.
    """
    basic_spec.local_tools = [
        LocalToolInfo(
            name="broken",
            path=None,
            language="python",
            runtime=ToolRuntime.SERVER,
        ),
    ]
    with pytest.raises(OmnigentError, match=r"server-runtime tool has no"):
        agent_spec_to_agent_def(basic_spec)


def test_client_runtime_tool_skips_cancellable_check(
    basic_spec: AgentSpec,
) -> None:
    """
    ``_reject_unsupported_concepts`` walks every ``local_tools``
    entry and would call :func:`_is_cancellable_function_path`
    on its ``path``. Client-runtime tools have ``path=None`` —
    the check must short-circuit, not pass ``None`` into the
    string-prefix detector.

    This test wouldn't fail in isolation (the cancellable
    check is permissive), but a regression that drops the
    ``path is None`` skip would cause an ``AttributeError`` /
    ``TypeError`` inside the detector before the translator
    gets a chance to handle the client-runtime branch. Pin the
    happy path here to keep that skip in place.

    What breaks if this fails: every client-runtime spec
    starts crashing in the rejection helper before reaching
    the translator's client-runtime branch.
    """
    basic_spec.local_tools = [
        LocalToolInfo(
            name="open_in_editor",
            path=None,
            language="python",
            runtime=ToolRuntime.CLIENT,
            parameters={"type": "object", "properties": {}},
        ),
    ]
    # No exception — translation completes and the client tool
    # makes it through unchanged.
    agent_def = agent_spec_to_agent_def(basic_spec)
    assert agent_def.tools["open_in_editor"].runtime == "client"


def test_client_runtime_tool_round_trips_through_reverse_translation(
    basic_spec: AgentSpec,
) -> None:
    """
    A client-runtime ``LocalToolInfo`` survives a forward+reverse
    pass: ``agent_def_to_agent_spec(agent_spec_to_agent_def(s))``
    preserves ``runtime=ToolRuntime.CLIENT``, ``path=None``, and
    the explicit ``parameters`` block.

    What breaks if this fails: a YAML save/load cycle silently
    rewrites a client tool as a server tool (or drops its
    parameters), and the next load either explodes (no callable
    to resolve) or silently demotes the tool to a no-args
    server tool that never runs.
    """
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    basic_spec.local_tools = [
        LocalToolInfo(
            name="open_in_editor",
            path=None,
            language="python",
            runtime=ToolRuntime.CLIENT,
            parameters=parameters,
        ),
    ]
    agent_def = agent_spec_to_agent_def(basic_spec)
    round_tripped = agent_def_to_agent_spec(agent_def)
    [tool_info] = round_tripped.local_tools
    # Discriminator survived the reverse direction — this is the
    # bit the runtime branch keys off of.
    assert tool_info.runtime == ToolRuntime.CLIENT
    # No path was invented on the way back. If a non-None path
    # appears, the validator's "client tool must NOT declare a
    # callable" rule will reject the round-tripped spec.
    assert tool_info.path is None
    # Parameters preserved — without these the validator rejects
    # the round-tripped spec ("client tool has no parameters").
    assert tool_info.parameters == parameters
