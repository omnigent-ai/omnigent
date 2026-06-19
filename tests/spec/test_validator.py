"""Tests for omnigent.spec.validator."""

from __future__ import annotations

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.spec.types import (
    AgentSpec,
    CompactionConfig,
    ExecutorSpec,
    InteractionConfig,
    LLMConfig,
    LocalToolInfo,
    MCPServerConfig,
    ModalityConfig,
    SkillSpec,
    ToolsConfig,
)
from omnigent.spec.validator import validate


def _minimal_spec(**overrides: object) -> AgentSpec:
    """Build a minimal valid AgentSpec with optional overrides.

    Mirrors the parser's consolidation: when ``llm`` is supplied,
    ``executor.model`` and ``executor.connection`` are synced from
    the LLM config so the validator sees the canonical fields.
    """
    defaults: dict[str, object] = {
        "spec_version": 1,
        "executor": ExecutorSpec(config={"harness": "claude-sdk"}),
    }
    defaults.update(overrides)
    # Consolidate llm → executor (same as parser does at load time).
    llm = defaults.get("llm")
    if isinstance(llm, LLMConfig):
        executor = defaults.get("executor")
        if not isinstance(executor, ExecutorSpec):
            executor = ExecutorSpec(config={"harness": "claude-sdk"})
        if executor.model is None and llm.model:
            executor = ExecutorSpec(
                type=executor.type,
                timeout=executor.timeout,
                max_iterations=executor.max_iterations,
                profile=executor.profile,
                config=executor.config,
                model=llm.model,
                connection=llm.connection if executor.connection is None else executor.connection,
                context_window=executor.context_window,
            )
        defaults["executor"] = executor
    return AgentSpec(**defaults)  # type: ignore[arg-type]


def test_minimal_spec_valid() -> None:
    result = validate(_minimal_spec())
    assert result.valid


def test_invalid_spec_version() -> None:
    result = validate(_minimal_spec(spec_version=2))
    assert not result.valid
    assert any("spec_version" in e.path for e in result.errors)


def test_llm_valid() -> None:
    spec = _minimal_spec(llm=LLMConfig(model="openai/gpt-5.4", connection={"api_key": "sk-test"}))
    result = validate(spec)
    assert result.valid


def test_llm_empty_model() -> None:
    spec = _minimal_spec(llm=LLMConfig(model=""))
    result = validate(spec)
    assert not result.valid
    assert any("llm.model" in e.path for e in result.errors)


def test_llm_arbitrary_extra_passes_validation() -> None:
    """Extra keys are passed through — validator does not reject them."""
    spec = _minimal_spec(
        llm=LLMConfig(
            model="openai/gpt-5.4",
            connection={"api_key": "sk-test"},
            extra={"temperature": 0.7, "reasoning_effort": "extreme"},
        )
    )
    result = validate(spec)
    assert result.valid


def test_valid_input_modalities() -> None:
    spec = _minimal_spec(
        interaction=InteractionConfig(
            modalities=ModalityConfig(input=["text", "image", "audio", "video", "file"])
        )
    )
    result = validate(spec)
    assert result.valid


def test_invalid_input_modality() -> None:
    spec = _minimal_spec(
        interaction=InteractionConfig(modalities=ModalityConfig(input=["text", "smell"]))
    )
    result = validate(spec)
    assert not result.valid
    assert any("smell" in e.message for e in result.errors)


def test_invalid_output_modality() -> None:
    spec = _minimal_spec(
        interaction=InteractionConfig(modalities=ModalityConfig(output=["text", "file"]))
    )
    result = validate(spec)
    assert not result.valid
    assert any("file" in e.message for e in result.errors)


def test_valid_output_modalities() -> None:
    spec = _minimal_spec(
        interaction=InteractionConfig(modalities=ModalityConfig(output=["text", "image", "audio"]))
    )
    result = validate(spec)
    assert result.valid


def test_skill_valid() -> None:
    spec = _minimal_spec(
        skills=[
            SkillSpec(
                name="deep-search",
                description="Search the web.",
                content="Use search.web.",
            )
        ]
    )
    result = validate(spec)
    assert result.valid


def test_skill_name_invalid_pattern() -> None:
    spec = _minimal_spec(skills=[SkillSpec(name="Bad_Name", description="Bad.", content=".")])
    result = validate(spec)
    assert not result.valid
    assert any("must match" in e.message for e in result.errors)


def test_skill_name_too_long() -> None:
    spec = _minimal_spec(skills=[SkillSpec(name="a" * 65, description="Long name.", content=".")])
    result = validate(spec)
    assert not result.valid
    assert any("at most 64" in e.message for e in result.errors)


def test_skill_description_too_long() -> None:
    spec = _minimal_spec(skills=[SkillSpec(name="ok", description="x" * 1025, content=".")])
    result = validate(spec)
    assert not result.valid
    assert any("at most 1024" in e.message for e in result.errors)


def test_duplicate_skill_names() -> None:
    spec = _minimal_spec(
        skills=[
            SkillSpec(name="dupe", description="First.", content="."),
            SkillSpec(name="dupe", description="Second.", content="."),
        ]
    )
    result = validate(spec)
    assert not result.valid
    assert any("duplicate skill name" in e.message for e in result.errors)


def test_mcp_http_valid() -> None:
    spec = _minimal_spec(mcp_servers=[MCPServerConfig(name="svc", url="http://localhost:9000")])
    result = validate(spec)
    assert result.valid


def test_duplicate_mcp_names() -> None:
    spec = _minimal_spec(
        mcp_servers=[
            MCPServerConfig(name="dupe", url="http://a"),
            MCPServerConfig(name="dupe", url="http://b"),
        ]
    )
    result = validate(spec)
    assert not result.valid
    assert any("duplicate MCP server name" in e.message for e in result.errors)


def test_duplicate_tool_names_across_mcp_and_local() -> None:
    spec = _minimal_spec(
        mcp_servers=[MCPServerConfig(name="search", url="http://localhost:9000")],
        local_tools=[
            LocalToolInfo(name="search", path="tools/python/search.py", language="python")
        ],
    )
    result = validate(spec)
    assert not result.valid
    assert any("duplicate tool name" in e.message for e in result.errors)


def test_sub_agent_reference_valid() -> None:
    sub = _minimal_spec(
        name="helper",
        llm=LLMConfig(model="openai/gpt-4o", connection={"api_key": "sk-test"}),
    )
    spec = _minimal_spec(
        tools=ToolsConfig(agents=["helper"]),
        sub_agents=[sub],
    )
    result = validate(spec)
    assert result.valid


def test_sub_agent_reference_missing() -> None:
    spec = _minimal_spec(
        tools=ToolsConfig(agents=["ghost"]),
    )
    result = validate(spec)
    assert not result.valid
    assert any("ghost" in e.message for e in result.errors)


@pytest.mark.parametrize(
    "invalid_name",
    [
        "has.dot",  # dot is the tunneled model field delimiter
        "has/slash",  # slash is the litellm provider/model separator
        "has space",  # whitespace confuses API clients and log pipelines
        "has\ttab",  # tab is also whitespace
        "",  # empty string has no meaningful identity
    ],
)
def test_agent_name_invalid_characters(invalid_name: str) -> None:
    """
    Agent names with dots, slashes, whitespace, or empty string are rejected.

    Each of these characters would break either the tunneled model field
    (dots), litellm routing (slashes), or client parsing (whitespace/empty).
    """
    spec = _minimal_spec(name=invalid_name)
    result = validate(spec)
    assert not result.valid
    assert any("name" in e.path for e in result.errors)


@pytest.mark.parametrize(
    "valid_name",
    [
        "researcher",
        "my-agent",
        "agent_v2",
        "Agent123",
        "CamelCase",
        "a",
    ],
)
def test_agent_name_valid(valid_name: str) -> None:
    """Agent names using alphanumeric, hyphens, and underscores are accepted."""
    spec = _minimal_spec(name=valid_name)
    result = validate(spec)
    assert result.valid


def test_agent_name_invalid_in_sub_agent() -> None:
    """Invalid name on a sub-agent (not just the root) is caught."""
    sub = _minimal_spec(
        name="bad.name",
        llm=LLMConfig(model="openai/gpt-4o", connection={"api_key": "sk-test"}),
    )
    spec = _minimal_spec(
        tools=ToolsConfig(agents=["bad.name"]),
        sub_agents=[sub],
    )
    result = validate(spec)
    assert not result.valid
    assert any("name" in e.path for e in result.errors)


def test_agent_name_reserved_ui_rejected() -> None:
    """
    The reserved name ``"ui"`` is rejected even though it matches the
    name pattern.

    ``"ui"`` is the Web UI "Add agent" title-prefix sentinel
    (``"ui:<agent>:<label>"``); a sub-agent named ``"ui"`` would be
    misparsed as a user-added child by the child-session summary. The
    validator fails loud so the collision can never reach the store.
    Asserts on ``"reserved"`` in the message (not just ``"name"`` in the
    path) so a generic pattern rejection can't masquerade as this check.
    """
    spec = _minimal_spec(name="ui")
    result = validate(spec)
    assert not result.valid
    assert any("reserved" in e.message for e in result.errors)


def test_agent_name_reserved_ui_rejected_in_sub_agent() -> None:
    """A sub-agent named ``"ui"`` is rejected, not just the root."""
    sub = _minimal_spec(
        name="ui",
        llm=LLMConfig(model="openai/gpt-4o", connection={"api_key": "sk-test"}),
    )
    spec = _minimal_spec(
        tools=ToolsConfig(agents=["ui"]),
        sub_agents=[sub],
    )
    result = validate(spec)
    assert not result.valid
    assert any("reserved" in e.message for e in result.errors)


def test_multiple_errors_reported() -> None:
    """
    Validator reports all errors, not just the first.

    Three violations: spec_version != 1, skill name not lowercase,
    and skill description exceeds 1024 chars.
    """
    spec = _minimal_spec(
        spec_version=99,
        skills=[
            SkillSpec(name="BAD", description="x" * 2000, content="."),
        ],
    )
    result = validate(spec)
    assert not result.valid
    # spec_version error + skill name pattern error + skill description length error
    assert len(result.errors) >= 3


# ── agents_sdk executor validation ────────────────────────


def test_agents_sdk_rejects_compaction() -> None:
    """
    ``agents_sdk`` executor forbids ``compaction`` — the SDK
    manages context internally.
    """
    spec = _minimal_spec(
        executor=ExecutorSpec(type="agents_sdk"),
        compaction=CompactionConfig(),
    )
    result = validate(spec)
    assert not result.valid
    assert any("compaction" in e.path for e in result.errors), (
        f"Expected compaction error, got: {result.errors}"
    )
    # Verify actual error message content, not just path.
    assert any("agents_sdk" in e.message for e in result.errors), (
        f"Error message should mention 'agents_sdk': {result.errors}"
    )


def test_agents_sdk_accepts_connection() -> None:
    """
    ``agents_sdk`` executor allows ``llm.connection`` — unlike
    ``claude_sdk`` which forbids it. The SDK supports custom
    OpenAI clients with per-agent API keys.
    """
    spec = _minimal_spec(
        executor=ExecutorSpec(type="agents_sdk"),
        llm=LLMConfig(
            model="gpt-5.4",
            connection={"api_key": "sk-test"},
        ),
    )
    result = validate(spec)
    assert result.valid, f"Expected valid spec, got errors: {result.errors}"


def test_omnigent_executor_accepts_valid_harness() -> None:
    """
    ``omnigent`` executor with ``config.harness`` set to one of
    the four supported harnesses validates cleanly.

    Failure here means every valid spec is rejected — a complete
    break of the phase 1 integration.
    """
    spec = _minimal_spec(
        llm=LLMConfig(model="databricks-claude-sonnet-4-6"),
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-sdk", "profile": "test-profile"},
        ),
    )
    result = validate(spec)
    assert result.valid, f"Expected valid spec, got errors: {result.errors}"


def test_omnigent_executor_rejects_missing_harness() -> None:
    """
    ``omnigent`` executor without ``config.harness`` is rejected.

    Without the harness selector the omnigent factory cannot
    pick a backend and would silently fall back to a MockExecutor.
    The validator fails loud instead.
    """
    spec = _minimal_spec(
        llm=LLMConfig(model="databricks-claude-sonnet-4-6"),
        executor=ExecutorSpec(type="omnigent", config={}),
    )
    result = validate(spec)
    assert not result.valid
    assert any("executor.config.harness" in e.path for e in result.errors), (
        f"Expected executor.config.harness error, got: {result.errors}"
    )


def test_omnigent_executor_rejects_unknown_harness() -> None:
    """
    ``omnigent`` executor with a harness not in the allowed set
    is rejected with a message naming the allowed harnesses.
    """
    spec = _minimal_spec(
        llm=LLMConfig(model="databricks-claude-sonnet-4-6"),
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "totally-bogus"},
        ),
    )
    result = validate(spec)
    assert not result.valid
    assert any("totally-bogus" in e.message for e in result.errors), (
        f"Error message should name the offending harness: {result.errors}"
    )


def test_omnigent_executor_rejects_compaction() -> None:
    """
    ``omnigent`` executor forbids ``compaction`` — the inner
    harness manages context internally, so any compaction
    directive from the spec would be silently ignored.
    """
    spec = _minimal_spec(
        llm=LLMConfig(model="databricks-claude-sonnet-4-6"),
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-sdk"},
        ),
        compaction=CompactionConfig(),
    )
    result = validate(spec)
    assert not result.valid
    assert any("compaction" in e.path for e in result.errors), (
        f"Expected compaction error, got: {result.errors}"
    )


def test_mcp_stdio_valid() -> None:
    """
    Validator accepts a well-formed stdio MCP: transport='stdio',
    command set, no HTTP fields.

    What breaks if this fails: a stdio MCPServerConfig constructed
    programmatically (e.g. by the translator in
    spec/omnigent.py's _translate_mcp_tool_from_def) would fail
    validation at spec-load time even though it's correct.
    """
    spec = _minimal_spec(
        mcp_servers=[
            MCPServerConfig(
                name="github",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                env={"GITHUB_TOKEN": "ghp_xyz"},
            )
        ]
    )
    result = validate(spec)
    assert result.valid


def test_mcp_stdio_missing_command_invalid() -> None:
    """
    Validator rejects stdio MCP without command. Catches
    programmatic construction paths that bypass the parser's
    own missing-field check.
    """
    spec = _minimal_spec(
        mcp_servers=[MCPServerConfig(name="broken", transport="stdio", command=None)]
    )
    result = validate(spec)
    assert not result.valid
    assert any("required when transport is 'stdio'" in e.message for e in result.errors)


def test_mcp_stdio_with_url_invalid() -> None:
    """
    Validator rejects stdio MCP that also has an HTTP ``url``
    set. Matches the parser's wrong-transport-key rejection but
    runs on the post-parsed spec (catching test fixtures /
    translator output that construct MCPServerConfig directly).
    """
    spec = _minimal_spec(
        mcp_servers=[
            MCPServerConfig(
                name="mixed",
                transport="stdio",
                command="npx",
                url="http://stale.example",
            )
        ]
    )
    result = validate(spec)
    assert not result.valid
    assert any("not allowed when transport is 'stdio'" in e.message for e in result.errors)


def test_mcp_http_without_url_invalid() -> None:
    """
    Validator rejects HTTP MCP without ``url``. The default
    ``transport="http"`` + missing ``url`` is the shape produced
    by ``MCPServerConfig(name="x")`` — easy mistake.
    """
    spec = _minimal_spec(mcp_servers=[MCPServerConfig(name="ghosted")])
    result = validate(spec)
    assert not result.valid
    assert any("required when transport is 'http'" in e.message for e in result.errors)


def test_mcp_http_with_stdio_field_invalid() -> None:
    """
    Validator rejects HTTP MCP that has a stdio-only field
    (``command``, ``args``, or ``env``) set. Symmetric coverage
    of the stdio-with-url test — either direction of mistaken
    transport mixing must fail validation.
    """
    spec = _minimal_spec(
        mcp_servers=[
            MCPServerConfig(
                name="mixed",
                transport="http",
                url="http://mcp.example.com",
                command="npx",
                env={"X": "y"},
            )
        ]
    )
    result = validate(spec)
    assert not result.valid
    # Both the command and env fields trip separate error entries,
    # so at minimum one of them surfaces.
    assert any("not allowed when transport is 'http'" in e.message for e in result.errors)


# ---------------------------------------------------------------------------
# os_env sandbox combo checks.
# ---------------------------------------------------------------------------
# These mirror the loader / parser checks so an AgentSpec built
# programmatically — by tests, the omnigent compat shim, or any
# caller skipping the YAML pipeline — still gets the same validation
# guard before the spec reaches the runtime.


def _os_env(**sandbox_kwargs: object) -> OSEnvSpec:
    """Build an OSEnvSpec wrapping an OSEnvSandboxSpec from kwargs."""
    return OSEnvSpec(
        type="caller_process",
        cwd="/tmp/workspace",
        sandbox=OSEnvSandboxSpec(**sandbox_kwargs),  # type: ignore[arg-type]
    )


def test_os_env_egress_rules_requires_hard_enforcing_backend() -> None:
    """``egress_rules`` on ``sandbox.type=none`` is rejected — the
    ``none`` backend doesn't isolate the network namespace so the
    rules would be inert decoration on the policy. The error names
    both hard-enforcing backends so the spec author knows the fix.
    """
    spec = _minimal_spec(
        os_env=_os_env(
            type="none",
            egress_rules=["* api.github.com/**"],
        ),
    )
    result = validate(spec)
    assert not result.valid
    matches = [e for e in result.errors if e.path == "os_env.sandbox.egress_rules"]
    assert matches, f"expected egress_rules error, got: {result.errors}"
    message = matches[0].message
    assert "linux_bwrap" in message
    assert "darwin_seatbelt" in message
    assert "Fix:" in message
    assert "do not use sandbox.type=none with egress_rules" in message


def test_os_env_egress_rules_accepted_for_bwrap() -> None:
    """``egress_rules`` on ``linux_bwrap`` is allowed."""
    spec = _minimal_spec(
        os_env=_os_env(
            type="linux_bwrap",
            egress_rules=["* api.github.com/**"],
        ),
    )
    result = validate(spec)
    egress_errors = [e for e in result.errors if "egress" in e.path]
    assert egress_errors == [], (
        f"egress_rules on bwrap should pass validation, got: {result.errors}"
    )


def test_os_env_egress_rules_accepted_for_seatbelt() -> None:
    """``egress_rules`` on ``darwin_seatbelt`` is allowed."""
    spec = _minimal_spec(
        os_env=_os_env(
            type="darwin_seatbelt",
            egress_rules=["* api.github.com/**"],
        ),
    )
    result = validate(spec)
    egress_errors = [e for e in result.errors if "egress" in e.path]
    assert egress_errors == [], (
        f"egress_rules on seatbelt should pass validation, got: {result.errors}"
    )


def test_os_env_start_in_scratch_requires_active_sandbox() -> None:
    """``start_in_scratch`` with ``sandbox.type=none`` is rejected
    because there's no scratch tmpdir to chdir into.
    """
    spec = _minimal_spec(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd="/tmp/workspace",
            sandbox=OSEnvSandboxSpec(type="none"),
            start_in_scratch=True,
        ),
    )
    result = validate(spec)
    assert not result.valid
    assert any(e.path == "os_env.start_in_scratch" for e in result.errors), (
        f"expected start_in_scratch error, got: {result.errors}"
    )


def test_os_env_start_in_scratch_and_fork_mutually_exclusive() -> None:
    """``start_in_scratch`` and ``fork`` are mutually exclusive —
    fork already provides a writable workspace by copying cwd.
    """
    spec = _minimal_spec(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd="/tmp/workspace",
            sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
            fork=True,
            start_in_scratch=True,
        ),
    )
    result = validate(spec)
    assert not result.valid
    matches = [e for e in result.errors if e.path == "os_env.start_in_scratch"]
    assert matches, f"expected start_in_scratch + fork error, got: {result.errors}"
    assert "mutually exclusive" in matches[0].message


def test_os_env_no_validation_when_absent() -> None:
    """``os_env`` is optional — when absent, the validator is a no-op
    and the spec stays valid against an otherwise minimal shape.
    """
    spec = _minimal_spec()
    result = validate(spec)
    os_env_errors = [e for e in result.errors if e.path.startswith("os_env")]
    assert os_env_errors == [], f"absent os_env should not produce errors, got: {result.errors}"
