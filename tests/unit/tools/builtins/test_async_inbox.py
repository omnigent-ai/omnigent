"""
Unit tests for ``sys_call_async`` (and the future async-inbox
builtins added in 11a.ii / 11a.iii).

Covers the schema shape and the validation paths in
:meth:`SysCallAsyncTool.dispatch_async` that don't require a
running DBOS workflow:

- Bad arguments (malformed JSON, missing fields, wrong types).
- Self-dispatch rejection.
- Unknown target tool.
- Unsupported target tool kind (non-LocalPythonTool).

The full happy path — dispatching a real LocalPythonTool through
``_dispatch_local_python_tool_async`` — is exercised by the
existing async-tool integration tests at
``tests/server/integration/test_async_tool_integration.py`` once
the registration gating in 11a.i has been hooked up. We don't
duplicate that path here because it requires DBOS + a workflow
context.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.spec import AgentSpec
from omnigent.tools.builtins.async_inbox import (
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysCancelTaskTool,
    SysReadInboxTool,
)
from omnigent.tools.manager import ToolManager


@pytest.fixture()
def tool() -> SysCallAsyncTool:
    """Single :class:`SysCallAsyncTool` instance — stateless, reusable."""
    return SysCallAsyncTool()


# ── Schema tests ──────────────────────────────────────────


def test_schema_required_fields_and_no_extra_props(tool: SysCallAsyncTool) -> None:
    """
    The schema requires ``tool`` and ``args`` and rejects unknown
    properties.

    A regression here would either let the LLM omit a required
    field (rejected at dispatch with a confusing
    ``missing_required`` error instead of a schema error) or
    accept arbitrary extras (which the validator would then drop
    rather than pass through to the handler).
    """
    schema = tool.get_schema()["function"]["parameters"]
    assert schema["required"] == ["tool", "args"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"].keys()) == {"tool", "args"}


def test_is_async_always_true(tool: SysCallAsyncTool) -> None:
    """
    ``is_async`` returns ``True`` regardless of arguments — the whole
    point of this tool is async dispatch.

    A regression where ``is_async`` returned ``False`` would route
    AP's ``_call_tool`` to :meth:`SysCallAsyncTool.invoke` (the
    sync path) which only returns an internal-routing error
    sentinel. Dispatch would never actually happen.
    """
    assert tool.is_async() is True
    assert tool.is_async("anything") is True


# ── dispatch_async failure path ───────────────────────────


def test_dispatch_async_raises_not_implemented_in_sessions_native_mode(
    tool: SysCallAsyncTool,
) -> None:
    """
    ``dispatch_async`` fails loud after the DBOS removal.

    The previous implementation resolved the target tool by
    name and started one of three background workflows
    (``background_tool_workflow`` for ``LocalPythonTool``,
    ``background_callable_tool_workflow`` for
    ``LocalCallableTool``, ``client_tool_workflow`` for
    ``ClientSideTool``) via the durability layer. Those
    workflows and their dispatch helpers were deleted in the
    sessions-native cutover; the sessions-native equivalent has
    not been built yet, so the tool raises
    ``NotImplementedError`` to make the gap loud rather than
    silently no-oping.

    The argument-validation branches (invalid JSON, missing
    ``tool`` / ``args``, self-dispatch rejection,
    unknown-tool, unsupported-tool-kind) all lived AFTER the
    dispatch entry; they're unreachable until a sessions-native
    dispatch surface is wired and will be re-pinned then.
    """
    import asyncio

    with pytest.raises(NotImplementedError, match="did not override dispatch_async"):
        asyncio.run(
            tool.dispatch_async(
                parent_task_id="task_parent",
                parent_conversation_id="conv_parent",
                agent_id="ag_test",
                agent_name="caller",
                arguments=json.dumps({"tool": "anything", "args": "{}"}),
                workspace_path=None,
            )
        )


# ── SysReadInboxTool ──────────────────────────────────────


@pytest.fixture()
def read_tool() -> SysReadInboxTool:
    """Single :class:`SysReadInboxTool` instance — stateless, reusable."""
    return SysReadInboxTool()


def test_read_inbox_schema_takes_no_arguments(read_tool: SysReadInboxTool) -> None:
    """
    The schema declares zero properties and rejects extras.

    A regression that added a parameter would let the LLM pass
    arguments the dispatcher silently ignores; a regression that
    dropped ``additionalProperties: false`` would let the LLM
    submit garbage that the framework swallows. Both surface as
    confusing behavior at run time, so pin the shape here.
    """
    schema = read_tool.get_schema()["function"]["parameters"]
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False
    # No ``required`` field expected — the empty-properties shape
    # is the canonical "no args" form OpenAI accepts.
    assert "required" not in schema or schema["required"] == []


def test_read_inbox_is_async_always_true(read_tool: SysReadInboxTool) -> None:
    """
    ``is_async`` returns ``True`` regardless of arguments.

    The drain reads from a DBOS topic via ``dbos_recv_async``,
    which only works in async context. Returning ``False`` here
    would route the call to the sync ``_call_tool`` thread-pool
    path — no event loop, ``dbos_recv_async`` raises, the LLM
    sees a confusing error.
    """
    assert read_tool.is_async() is True
    assert read_tool.is_async("anything") is True


def test_read_inbox_dispatch_async_raises_not_implemented(
    read_tool: SysReadInboxTool,
) -> None:
    """
    ``sys_read_inbox.dispatch_async`` fails loud after the DBOS removal.

    The previous implementation drained the workflow's
    ``async_work_complete`` DBOS topic via
    ``_drain_async_completions`` and rendered each payload via
    ``_format_async_completion_text``. The topic, the drain
    helper, and the formatter all lived inside the durability
    layer; they were removed in the sessions-native cutover.
    Until a sessions-native inbox is implemented, the LLM
    should not be offered ``sys_read_inbox`` and any accidental
    invocation must fail loud rather than return an empty
    string the LLM might misinterpret as "nothing back yet".
    """
    import asyncio

    with pytest.raises(NotImplementedError, match="did not override dispatch_async"):
        asyncio.run(
            read_tool.dispatch_async(
                parent_task_id="task_parent",
                parent_conversation_id="conv_parent",
                agent_id="ag_test",
                agent_name="sys_read_inbox",
                arguments="{}",
                workspace_path=None,
            )
        )


# ── SysCancelAsyncTool ────────────────────────────────────


@pytest.fixture()
def cancel_tool() -> SysCancelAsyncTool:
    """Single :class:`SysCancelAsyncTool` instance — stateless, reusable."""
    return SysCancelAsyncTool()


def test_cancel_async_subclasses_sys_cancel_task(
    cancel_tool: SysCancelAsyncTool,
) -> None:
    """
    ``SysCancelAsyncTool`` IS-A ``SysCancelTaskTool``.

    The subclass relationship is the contract: by inheriting,
    ``sys_cancel_async`` reuses every per-kind cancel primitive
    (terminal SIGINT, ``client_tool`` SSE emission, generic
    ``task_store.cancel``, the ``already_terminal`` short-circuit).
    A regression that turned this into composition or copied the
    cancel logic would silently drift from the parent's behaviour
    on terminal/client_tool kinds — those code paths only fire on
    the parent's class today, so the subclass linkage is what
    keeps them reachable from the alias.
    """
    assert isinstance(cancel_tool, SysCancelTaskTool)


def test_cancel_async_schema_uses_handle_id(
    cancel_tool: SysCancelAsyncTool,
) -> None:
    """
    Schema declares a single required ``handle_id`` string and
    rejects extras.

    The parameter rename from ``task_id`` (parent) to ``handle_id``
    is the one place the schema differs — pin it. A regression
    that reverted to ``task_id`` would still work (since invoke
    delegates with task_id JSON), but the LLM's mental model
    breaks: ``sys_call_async`` returns a "handle", and the cancel
    parameter should match that vocabulary.
    """
    schema = cancel_tool.get_schema()["function"]["parameters"]
    assert schema["required"] == ["handle_id"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"].keys()) == {"handle_id"}


# ── Manager registration gating ───────────────────────────


def test_async_enabled_false_does_not_register() -> None:
    """
    With ``async_enabled=False`` the manager does NOT register
    any of the async-namespace builtins.

    The flag still works as a kill-switch even though the
    default is ``True``: an agent author who explicitly sets
    ``async: false`` must see the suppression honored. Without
    this test, a regression that hard-coded the registration
    to fire unconditionally would silently ignore opt-out.
    """
    spec = AgentSpec(spec_version=1, async_enabled=False)
    manager = ToolManager(spec=spec)
    names = manager.get_tool_names()
    assert SysCallAsyncTool.name() not in names
    assert SysReadInboxTool.name() not in names
    assert SysCancelAsyncTool.name() not in names


def test_async_enabled_true_registers() -> None:
    """
    With ``async_enabled=True`` (the default) the manager
    registers all three async-namespace builtins and surfaces
    them in ``get_tool_schemas``.

    Schema is exposed so the LLM sees the tools in its function
    list. A regression where the registration ran but the schema
    didn't appear would manifest as the LLM seeing a tool name
    it can't use.
    """
    spec = AgentSpec(spec_version=1, async_enabled=True)
    manager = ToolManager(spec=spec)
    names = manager.get_tool_names()
    assert SysCallAsyncTool.name() in names
    assert SysReadInboxTool.name() in names
    assert SysCancelAsyncTool.name() in names
    schema_names = {s["function"]["name"] for s in manager.get_tool_schemas()}
    assert SysCallAsyncTool.name() in schema_names
    assert SysReadInboxTool.name() in schema_names
    assert SysCancelAsyncTool.name() in schema_names


def test_sys_cancel_task_always_registered_independently_of_async() -> None:
    """
    ``sys_cancel_task`` (the generic, task_id-keyed cancel) is
    always registered, regardless of ``async_enabled``.

    The async-namespace alias does NOT replace the generic tool —
    both coexist. Cancel for non-async-handle scenarios (terminal
    tasks, sub-agent tasks) is still task_id-keyed via
    ``sys_cancel_task``. A regression that gated the generic
    cancel on ``async: true`` would break terminal/sub-agent
    cancellation for agents that don't opt into async.
    """
    spec_off = AgentSpec(spec_version=1, async_enabled=False)
    manager_off = ToolManager(spec=spec_off)
    assert SysCancelTaskTool.name() in manager_off.get_tool_names()

    spec_on = AgentSpec(spec_version=1, async_enabled=True)
    manager_on = ToolManager(spec=spec_on)
    assert SysCancelTaskTool.name() in manager_on.get_tool_names()


# ── Spec parser wiring ────────────────────────────────────


def test_yaml_async_true_sets_flag(tmp_path: Any) -> None:
    """
    Top-level ``async: true`` in config.yaml lands on
    :attr:`AgentSpec.async_enabled`.

    Catches a regression where the parser silently drops the
    ``async`` key (e.g., if it's accidentally re-spelled
    ``async_enabled`` in the YAML schema). The YAML key is the
    LLM-spec-author-facing surface; the dataclass field name is
    internal.
    """
    from omnigent.spec import parse

    (tmp_path / "config.yaml").write_text("spec_version: 1\nasync: true\n")
    spec = parse(tmp_path)
    assert spec.async_enabled is True


def test_yaml_async_omitted_defaults_true(tmp_path: Any) -> None:
    """
    Specs that don't mention ``async:`` default to
    ``async_enabled=True``.

    Matches the legacy inner stack's default at
    ``omnigent/inner/datamodel.py::AgentDef.async_enabled``
    so the same YAML produces the same tool surface under
    Omnigent mode and the legacy path. Pinning this so a future
    parser refactor can't silently revert the default.
    """
    from omnigent.spec import parse

    (tmp_path / "config.yaml").write_text("spec_version: 1\n")
    spec = parse(tmp_path)
    assert spec.async_enabled is True


def test_yaml_async_false_disables(tmp_path: Any) -> None:
    """
    Top-level ``async: false`` lands on
    :attr:`AgentSpec.async_enabled` as ``False``.

    The flag is still a kill-switch — agents that explicitly
    don't want the async surface (e.g. for a minimal-tools
    demo agent) opt out via ``async: false``. The default is
    ``True``, but the off path must remain wired for the
    times an author needs it.
    """
    from omnigent.spec import parse

    (tmp_path / "config.yaml").write_text("spec_version: 1\nasync: false\n")
    spec = parse(tmp_path)
    assert spec.async_enabled is False
