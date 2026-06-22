"""Tests for the ``web_fetch`` built-in tool."""

from __future__ import annotations

from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    LLMConfig,
)
from omnigent.tools.builtins.web_fetch import (
    RESEARCHER_NAME,
    WebFetchTool,
    build_researcher_spec,
)

# ── Helpers ──────────────────────────────────────────


def _make_parent_spec(
    model: str = "openai/gpt-5.4",
    executor_type: str | None = None,
) -> AgentSpec:
    """
    Build a minimal parent AgentSpec for testing.

    :param model: The LLM model string.
    :param executor_type: Executor type override, or ``None``
        for default (llm).
    :returns: An AgentSpec suitable for constructing WebFetchTool.
    """
    executor = ExecutorSpec()
    if executor_type is not None:
        executor = ExecutorSpec(type=executor_type)
    return AgentSpec(
        spec_version=1,
        name="test-parent",
        llm=LLMConfig(model=model),
        executor=executor,
    )


# ── Schema ───────────────────────────────────────────


def test_web_fetch_schema_is_function() -> None:
    """Schema is a standard function schema with query + url params."""
    parent = _make_parent_spec()
    tool = WebFetchTool(parent_spec=parent)
    schema = tool.get_schema()
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "web_fetch"
    # query is required, url is optional.
    assert "query" in func["parameters"]["required"]
    assert "url" in func["parameters"]["properties"]
    assert "url" not in func["parameters"]["required"]


def test_web_fetch_name() -> None:
    """Tool name is 'web_fetch'."""
    assert WebFetchTool.name() == "web_fetch"


# ── Researcher spec ──────────────────────────────────


def test_researcher_inherits_parent_model() -> None:
    """
    The __web_researcher sub-agent must use the parent's LLM config.
    If it used a different model, the web_fetch tool would fail for
    agents using non-default providers (e.g. anthropic).
    """
    parent = _make_parent_spec(model="anthropic/claude-sonnet-4-20250514")
    tool = WebFetchTool(parent_spec=parent)
    researcher = tool.researcher_spec
    assert researcher.llm is not None, (
        "Researcher spec must have an llm block — "
        "without it, the workflow fails with 'no LLM configuration'."
    )
    assert researcher.llm.model == "anthropic/claude-sonnet-4-20250514", (
        f"Researcher should inherit parent model, got {researcher.llm.model!r}."
    )


def test_researcher_has_os_env_for_sys_os_shell() -> None:
    """
    The researcher must declare an ``os_env`` block — that's what
    registers ``sys_os_shell``, the only tool the researcher uses
    to fetch URLs (curl, python3 one-liners).

    What breaks if this fails: the researcher would have no shell
    primitive at all, can't fetch any URL, and ``web_fetch``
    silently degrades to "I cannot retrieve web content."
    """
    parent = _make_parent_spec()
    tool = WebFetchTool(parent_spec=parent)
    researcher = tool.researcher_spec
    # ``sys_os_shell`` registers when ``spec.os_env`` is non-None
    # (see ``ToolManager._register_os_env_tools``). The os_env
    # block on the researcher spec is what makes that registration
    # fire.
    assert researcher.os_env is not None, (
        "Researcher must declare os_env (got None) so the runtime "
        "registers sys_os_shell. Without it, the sub-agent has no "
        "shell primitive and can't fetch any URL."
    )


def test_researcher_inherits_parent_sandbox_egress() -> None:
    """
    The researcher must inherit the parent's ``os_env.sandbox`` so the
    parent's egress policy is enforced on the child's ``sys_os_shell``.

    Regression: ``build_researcher_spec`` previously
    hard-coded ``OSEnvSpec(type="caller_process")`` with ``sandbox=None``.
    Because ``create_os_environment`` only wires the MITM egress proxy
    from ``spec.sandbox`` (egress_rules / egress_allow_private_destinations),
    a sandbox-less child silently bypassed an egress-restricted parent's
    allowlist (e.g. reaching localhost / IMDS the parent blocked).
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    sandbox = OSEnvSandboxSpec(
        egress_rules=["GET api.example.com/**"],
        egress_allow_private_destinations=False,
    )
    parent = AgentSpec(
        spec_version=1,
        name="test-parent",
        llm=LLMConfig(model="openai/gpt-5.4"),
        os_env=OSEnvSpec(type="caller_process", sandbox=sandbox),
    )

    researcher = build_researcher_spec(parent)

    assert researcher.os_env is not None
    assert researcher.os_env.sandbox is not None, (
        "Researcher dropped the parent's sandbox — egress enforcement "
        "would be silently disabled for the web_fetch child."
    )
    assert researcher.os_env.sandbox.egress_rules == ["GET api.example.com/**"]
    assert researcher.os_env.sandbox.egress_allow_private_destinations is False


def test_researcher_os_env_without_parent_sandbox() -> None:
    """
    When the parent declares no os_env, the researcher still gets a
    valid os_env (so ``sys_os_shell`` registers) with no sandbox —
    matching the parent's (absent) policy rather than inventing one.
    """
    parent = _make_parent_spec()
    assert parent.os_env is None
    researcher = build_researcher_spec(parent)
    assert researcher.os_env is not None
    assert researcher.os_env.sandbox is None


def test_researcher_name_is_internal() -> None:
    """
    The researcher name must use __ prefix to avoid collision
    with user-declared sub-agent names.
    """
    parent = _make_parent_spec()
    tool = WebFetchTool(parent_spec=parent)
    assert tool.researcher_spec.name == RESEARCHER_NAME
    assert RESEARCHER_NAME.startswith("__"), (
        f"Internal sub-agent name should start with __, got {RESEARCHER_NAME!r}."
    )


def test_researcher_appended_to_parent_sub_agents() -> None:
    """
    After construction, the researcher spec must be in the parent's
    sub_agents list so _resolve_agent_spec_for_task can find it.
    """
    parent = _make_parent_spec()
    # sub_agents starts empty.
    assert len(parent.sub_agents) == 0
    WebFetchTool(parent_spec=parent)
    # Now it should have the researcher.
    names = [s.name for s in parent.sub_agents]
    assert RESEARCHER_NAME in names, f"Researcher should be in parent's sub_agents, got {names}."


def test_researcher_not_conversational() -> None:
    """
    The researcher should be non-conversational (one-shot task).
    """
    parent = _make_parent_spec()
    tool = WebFetchTool(parent_spec=parent)
    assert tool.researcher_spec.interaction.conversational is False


def test_researcher_has_instructions() -> None:
    """
    The researcher must have non-empty instructions that mention
    web research.
    """
    parent = _make_parent_spec()
    tool = WebFetchTool(parent_spec=parent)
    instructions = tool.researcher_spec.instructions
    assert instructions is not None
    # 100 chars minimum ensures non-trivial instructions. If shorter,
    # the researcher won't have enough guidance to know how to search
    # the web and extract content.
    assert len(instructions) > 100, (
        f"Researcher instructions too short ({len(instructions)} chars). "
        f"If < 100, the sub-agent won't have enough context to perform "
        f"web research effectively."
    )
    assert "web" in instructions.lower()


# ── Runner-side dispatch ─────────────────────────────


def test_web_fetch_is_runner_dispatched() -> None:
    """
    ``web_fetch`` must be in the runner's local-dispatch set.

    The Tool itself owns only the schema and the researcher
    sub-agent spec; the actual spawn runs through
    ``_execute_subagent_tool`` from
    ``omnigent/runner/tool_dispatch.py``. If a future change
    drops web_fetch from ``_ALL_LOCAL_TOOLS`` the LLM would call
    ``Tool.invoke`` which now raises ``NotImplementedError`` — a
    silent regression. Pinning the membership here keeps the two
    sides honest.
    """
    from omnigent.runner.tool_dispatch import should_dispatch_locally

    assert should_dispatch_locally("web_fetch") is True


def test_runner_handler_validates_query_required() -> None:
    """
    The runner handler returns the standard "query is required"
    error when the LLM omits ``query``.

    This is the validation web_fetch's old ``invoke`` used to
    perform; after the sessions-native migration it lives in
    ``_execute_web_fetch_tool``. Tested end-to-end here so a
    future migration that re-routes around the handler can't
    silently drop the validation.
    """
    import asyncio

    from omnigent.runner.tool_dispatch import _execute_web_fetch_tool

    result = asyncio.run(
        _execute_web_fetch_tool(
            args={},
            server_client=None,
            conversation_id="conv_t",
            agent_spec=None,
            task_id="t1",
        )
    )
    assert "query" in result.lower()


# ── build_researcher_spec standalone ────────────────


def testbuild_researcher_spec_copies_llm() -> None:
    """
    build_researcher_spec must copy the parent's LLM config
    exactly — same model string, same object reference for
    connection details.
    """
    llm = LLMConfig(
        model="groq/llama-4-scout",
        connection={"api_key": "test-key"},
    )
    parent = AgentSpec(spec_version=1, llm=llm)
    researcher = build_researcher_spec(parent)
    # Same LLM config object (reference copy, not deep copy —
    # the researcher doesn't modify it).
    assert researcher.llm is parent.llm
    assert researcher.llm.model == "groq/llama-4-scout"


def testbuild_researcher_spec_default_executor() -> None:
    """Researcher should use default executor (omnigent)."""
    parent = _make_parent_spec()
    researcher = build_researcher_spec(parent)
    assert researcher.executor.type == "omnigent"


def test_web_fetch_is_sync_in_sessions_native_mode() -> None:
    """
    ``web_fetch.is_async()`` returns ``False`` after the DBOS removal.

    The previous async-dispatch path spawned a ``kind="tool"``
    background DBOS workflow per fetch via
    ``_dispatch_server_tool_async``; that helper and the workflow
    were deleted with the durability layer. Until a sessions-native
    async dispatch surface is wired, ``web_fetch`` runs through
    the synchronous ``invoke`` path.
    """
    parent = _make_parent_spec()
    tool = WebFetchTool(parent_spec=parent)
    assert tool.is_async() is False
    # ``dispatch_async`` is no longer overridden — the base
    # ``Tool.dispatch_async`` raises ``NotImplementedError``.
    # Calling it would be a routing bug because ``is_async`` is
    # False; we don't exercise that path here.
