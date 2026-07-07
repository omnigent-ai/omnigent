"""Tests for the ``web_fetch`` built-in tool."""

from __future__ import annotations

import shutil
import sys

import pytest

from omnigent.errors import OmnigentError
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    LLMConfig,
    ProviderAuth,
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
        for default (omnigent executor on the claude-sdk harness).
    :returns: An AgentSpec suitable for constructing WebFetchTool.
    """
    # A real bootable ``type="omnigent"`` agent always carries a harness in
    # ``executor.config`` — without one ``harness_kind`` is the unspawnable
    # literal "omnigent". Default the helper to a real harness so fixtures build
    # bootable parents (and ``build_researcher_spec`` does not fail loud).
    executor = ExecutorSpec(config={"harness": "claude-sdk"})
    if executor_type is not None:
        executor = ExecutorSpec(type=executor_type)
    return AgentSpec(
        spec_version=1,
        name="test-parent",
        llm=LLMConfig(model=model),
        executor=executor,
    )


@pytest.fixture(autouse=True)
def _default_sandbox_binary_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep the seed-time sandbox probe host-independent for the suite.

    ``build_researcher_spec`` now calls ``shutil.which`` against the real
    host PATH for a no-``os_env`` parent (see
    ``_ensure_default_sandbox_runnable``). Unit tests must not depend on
    bubblewrap / ``sandbox-exec`` being installed on the runner, so default
    the probe to "binary present". The probe-specific tests below override
    this with their own ``monkeypatch.setattr(shutil, "which", ...)``.
    """
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")


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
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
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


def test_no_os_env_parent_fails_at_build_when_bwrap_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A no-os_env parent's researcher inherits the platform-default
    ``linux_bwrap`` sandbox, so a missing ``bwrap`` binary must fail
    at spec-build time pointing at the host dependency.

    Regression (#2068): the probe only ran at spawn time, deep in the
    run, and the error told the user to set ``os_env.sandbox.type`` —
    unreachable for a spawn-only parent, which cannot add an
    ``os_env`` block without also registering OS tools on itself.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    parent = _make_parent_spec()
    assert parent.os_env is None
    with pytest.raises(OmnigentError, match="bubblewrap") as excinfo:
        build_researcher_spec(parent)
    assert "sandbox.type" not in str(excinfo.value)


def test_no_os_env_parent_builds_when_bwrap_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``bwrap`` on PATH the no-os_env spec builds as before."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/bwrap")
    researcher = build_researcher_spec(_make_parent_spec())
    assert researcher.os_env is not None


def test_parent_with_os_env_skips_bwrap_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A parent that declares its own ``os_env`` keeps the inherit-
    verbatim path: its sandbox posture is its own to configure, so the
    probe must not second-guess it.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    parent = AgentSpec(
        spec_version=1,
        name="test-parent",
        llm=LLMConfig(model="openai/gpt-5.4"),
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )
    researcher = build_researcher_spec(parent)
    assert researcher.os_env is not None
    assert researcher.os_env.sandbox is not None
    assert researcher.os_env.sandbox.type == "none"


def test_no_os_env_parent_fails_at_build_when_sandbox_exec_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same seed-time probe covers the macOS default sandbox."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with pytest.raises(OmnigentError, match="sandbox-exec") as excinfo:
        build_researcher_spec(_make_parent_spec())
    assert "sandbox.type" not in str(excinfo.value)


def test_no_os_env_parent_builds_when_sandbox_exec_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``sandbox-exec`` on PATH the no-os_env spec builds on macOS."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/sandbox-exec")
    researcher = build_researcher_spec(_make_parent_spec())
    assert researcher.os_env is not None


def test_windows_platform_skips_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``windows_jobobject`` drives kernel Job Objects through ``ctypes``
    with no external binary, so there is nothing to probe on Windows.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    researcher = build_researcher_spec(_make_parent_spec())
    assert researcher.os_env is not None


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
    parent = AgentSpec(
        spec_version=1,
        llm=llm,
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
    )
    researcher = build_researcher_spec(parent)
    # Same LLM config object (reference copy, not deep copy —
    # the researcher doesn't modify it).
    assert researcher.llm is parent.llm
    assert researcher.llm.model == "groq/llama-4-scout"


def testbuild_researcher_spec_default_executor() -> None:
    """Researcher inherits the parent's omnigent executor type AND harness."""
    parent = _make_parent_spec()
    researcher = build_researcher_spec(parent)
    assert researcher.executor.type == "omnigent"
    # The harness carries from the parent so the child is bootable (not the
    # unspawnable literal "omnigent").
    assert researcher.executor.harness_kind == "claude-sdk"


def test_researcher_build_fails_loud_when_parent_has_no_harness() -> None:
    """
    A parent with ``ExecutorSpec(type="omnigent", config={})`` (no harness)
    must NOT silently produce an unbootable child whose
    ``harness_kind == "omnigent"`` — it must fail loud at build time.

    The child ``__web_researcher`` session is created without a per-session
    ``harness_override``, so the runner resolves its harness solely from this
    spec. A child carrying the literal ``"omnigent"`` would crash the runner
    with ``unknown harness 'omnigent'`` (the original Layer-1 failure). The
    resolved harness (e.g. an API ``harness_override`` on a spec with no
    ``executor.config["harness"]``) is not visible at this call site, so
    ``build_researcher_spec`` raises a clear, parent-naming error instead.
    """
    import pytest

    from omnigent.errors import ErrorCode, OmnigentError

    parent = AgentSpec(
        spec_version=1,
        name="no-harness-leg",
        llm=LLMConfig(model="openai/gpt-5.4"),
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    with pytest.raises(OmnigentError) as exc_info:
        build_researcher_spec(parent)

    # Actionable: names the offending parent leg and the missing harness.
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert "no-harness-leg" in str(exc_info.value)
    assert "harness" in str(exc_info.value).lower()


def test_researcher_inherits_parent_harness_auth_and_model() -> None:
    """
    Regression: the reconstructed ``__web_researcher`` must run on the
    PARENT LEG's harness with the parent's credentials and model.

    ``build_researcher_spec`` previously copied only ``llm`` and built a
    bare ``ExecutorSpec(max_iterations=5)``. That bare spec defaults
    ``type`` to ``"omnigent"`` with an empty ``config``, so:

    - Layer 1 (active): ``executor.harness_kind`` resolves to the literal
      ``"omnigent"`` (no ``config["harness"]``), and the runner aborts the
      researcher spawn with ``RuntimeError: unknown harness 'omnigent'``
      before any model routing — every ``web_fetch`` fails on all legs.
    - Layer 2 (latent): dropping the parent's ``auth`` and model strips the
      researcher off the parent's provider, so a gateway model such as
      ``z-ai/glm-5.2`` hits the native router with ``Unknown provider
      'z-ai'`` and the codex / claude legs fail on missing credentials.

    The harness spawn-env builders read ``executor.config["harness"]``
    (harness selection), ``executor.model`` (NOT ``llm.model``), and
    ``executor.auth`` / ``executor.connection`` (credentials + endpoint
    overrides), so all of these must carry from the parent.
    """
    parent_auth = ProviderAuth(name="openrouter")
    parent = AgentSpec(
        spec_version=1,
        name="pi-parent",
        llm=LLMConfig(model="z-ai/glm-5.2"),
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "pi"},
            model="z-ai/glm-5.2",
            connection={"base_url": "https://openrouter.ai/api/v1"},
            auth=parent_auth,
        ),
    )

    researcher = build_researcher_spec(parent)

    # Layer 1: the child must NOT be the bare "unknown harness 'omnigent'"
    # spec — it carries the parent's harness selector.
    assert researcher.executor.config.get("harness") == "pi", (
        "Researcher dropped the parent's harness — the runner would abort "
        f"with unknown harness {researcher.executor.harness_kind!r}."
    )
    assert researcher.executor.harness_kind == "pi", (
        "harness_kind must resolve to the parent's harness, not the literal "
        f"executor type; got {researcher.executor.harness_kind!r}."
    )
    assert researcher.executor.harness_kind != "omnigent"

    # Layer 2: credentials + model must carry so the parent's provider routes
    # the parent's model.
    assert researcher.executor.auth is parent_auth, (
        "Researcher dropped the parent's auth — the gateway model would hit "
        "the native router (Unknown provider 'z-ai')."
    )
    assert researcher.executor.model == "z-ai/glm-5.2"
    assert researcher.executor.connection == {"base_url": "https://openrouter.ai/api/v1"}

    # The fast-cap is preserved.
    assert researcher.executor.max_iterations == 5


def test_researcher_drops_inline_os_env_from_executor_config() -> None:
    """
    ``executor.config["os_env"]`` is an inline-sub-spec translation artifact;
    the researcher declares its own top-level ``os_env``. Carrying the config
    ``os_env`` would re-introduce a stale sandbox mapping, so it is dropped
    while every other routing key (e.g. ``harness``) is preserved.
    """
    parent = AgentSpec(
        spec_version=1,
        name="codex-parent",
        llm=LLMConfig(model="openai/gpt-5.4"),
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex", "os_env": {"type": "caller_process"}},
            model="openai/gpt-5.4",
        ),
    )

    researcher = build_researcher_spec(parent)

    assert researcher.executor.config.get("harness") == "codex"
    assert "os_env" not in researcher.executor.config
    # The explicit top-level os_env (which registers sys_os_shell) is intact.
    assert researcher.os_env is not None


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
