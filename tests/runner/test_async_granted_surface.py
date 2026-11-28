"""``sys_call_async`` and ``execute_tool`` honour the agent spec's granted tool surface."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.inner.datamodel import OSEnvSpec
from omnigent.runner import tool_dispatch
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.asyncio


def _spawn_kwargs(spec: AgentSpec | None, harness: str | None = None) -> dict[str, Any]:
    return {
        "effective_harness": harness,
        "server_client": None,
        "terminal_registry": None,
        "resource_registry": None,
        "agent_spec": spec,
        "conversation_id": "conv_granted",
        "task_id": None,
        "agent_id": None,
        "agent_name": None,
        "runner_workspace": None,
        "mcp_manager": None,
        "filesystem_registry": None,
    }


async def _call_sys_call_async(
    spec: AgentSpec | None,
    target: str,
    *,
    inbox: asyncio.Queue[dict[str, Any]],
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]],
    harness: str | None = None,
) -> str:
    return await tool_dispatch.execute_tool(
        tool_name="sys_call_async",
        arguments=json.dumps({"tool": target, "args": "{}"}),
        agent_spec=spec,
        conversation_id="conv_granted",
        session_inbox=inbox,
        session_async_tasks=tasks,
        effective_harness=harness,
    )


async def test_sys_call_async_refuses_target_outside_granted_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only agent cannot reach ``sys_os_shell`` by naming it as an async target."""
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(spec_version=1, async_enabled=True)  # no os_env → no sys_os_*
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    out = await _call_sys_call_async(spec, "sys_os_shell", inbox=inbox, tasks=tasks)

    assert out.startswith("Error: sys_call_async refused:")
    assert "sys_os_shell" in out
    assert "not enabled" in out
    assert tasks == {}
    assert inbox.empty()
    await asyncio.sleep(0)
    assert executed == []


async def test_sys_call_async_granted_target_reports_through_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target the spec grants still runs in the background and lands in the inbox."""

    async def _fake_session_query(tool_name: str, *_a: Any, **_kw: Any) -> str:
        return json.dumps({"ran": tool_name})

    monkeypatch.setattr(tool_dispatch, "_execute_session_query_tool", _fake_session_query)
    spec = AgentSpec(spec_version=1, async_enabled=True)  # sys_session_list is always registered
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    handle = json.loads(
        await _call_sys_call_async(spec, "sys_session_list", inbox=inbox, tasks=tasks)
    )
    assert handle["status"] == "in_progress"
    bg_task, _evt = tasks[handle["handle_id"]]
    await bg_task

    item = inbox.get_nowait()
    assert item["handle_id"] == handle["handle_id"]
    assert item["tool_name"] == "sys_session_list"
    assert item["status"] == "completed"
    assert json.loads(item["output"]) == {"ran": "sys_session_list"}

    drained = await tool_dispatch.execute_tool(
        tool_name="sys_read_inbox",
        arguments="{}",
        agent_spec=spec,
        conversation_id="conv_granted",
        session_inbox=inbox,
        session_async_tasks=tasks,
    )
    assert drained == "Inbox is empty — no completed tasks."


async def test_sys_call_async_without_spec_is_not_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec-less dispatch has no granted surface to check and keeps working."""

    async def _fake(**_kw: Any) -> str:
        return "ok"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _fake)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    handle = json.loads(
        tool_dispatch._spawn_async_tool(
            {"tool": "sys_os_shell", "args": "{}"},
            session_inbox=inbox,
            session_async_tasks=tasks,
            **_spawn_kwargs(None),
        )
    )
    await tasks[handle["handle_id"]][0]
    assert inbox.get_nowait()["status"] == "completed"


async def test_execute_tool_refuses_ungranted_tool_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The membership check also guards direct ``execute_tool`` calls."""
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(spec_version=1)

    out = json.loads(
        await tool_dispatch.execute_tool(
            tool_name="sys_os_shell",
            arguments=json.dumps({"command": "id"}),
            agent_spec=spec,
        )
    )
    assert "sys_os_shell" in out["error"]
    assert "not enabled" in out["error"]
    assert executed == []


@pytest.mark.parametrize("harness", ["claude-native", "cursor-native", "kiro-native"])
@pytest.mark.parametrize("os_env", [None, OSEnvSpec()], ids=["no-os-env", "os-env"])
async def test_native_harness_refuses_sys_os_tools_on_every_path(
    monkeypatch: pytest.MonkeyPatch,
    os_env: OSEnvSpec | None,
    harness: str,
) -> None:
    """A native harness gets no ``sys_os_*`` grant.

    Its relay never offers them, so neither ``execute_tool`` nor a
    ``sys_call_async`` naming ``sys_os_shell`` may run them, even when the spec
    declares ``os_env``.
    """
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(
        spec_version=1,
        async_enabled=True,
        os_env=os_env,
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )

    direct = json.loads(
        await tool_dispatch.execute_tool(
            tool_name="sys_os_shell",
            arguments=json.dumps({"command": "id"}),
            agent_spec=spec,
        )
    )
    assert "not enabled" in direct["error"]

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    spawned = await _call_sys_call_async(spec, "sys_os_shell", inbox=inbox, tasks=tasks)
    assert spawned.startswith("Error: sys_call_async refused:")
    assert tasks == {}
    await asyncio.sleep(0)
    assert executed == []


async def test_granted_tool_names_cached_per_spec_instance() -> None:
    """The surface is computed once per live spec object and harness."""
    spec = AgentSpec(spec_version=1, async_enabled=True)
    first = tool_dispatch._granted_tool_names(spec)
    assert first is tool_dispatch._granted_tool_names(spec)
    assert {"sys_call_async", "sys_read_inbox", "sys_cancel_async"} <= first
    assert "sys_os_shell" not in first
    # A different effective harness is a different surface, so a different entry.
    native = tool_dispatch._granted_tool_names(spec, "claude-native")
    assert native is not first
    assert "sys_os_shell" not in native


async def test_granted_tool_names_follow_a_routing_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cached surface tracks the routing answer without anyone clearing it.

    ``sys_advise_models`` is granted only while routing answers, so a cached
    surface from before a flip would refuse the advisor call it now advertises.
    """
    from omnigent.runtime import _globals, set_remote_routing_available
    from omnigent.runtime.caps import RuntimeCaps

    monkeypatch.setattr(_globals, "_caps", RuntimeCaps())
    spec = AgentSpec(spec_version=1, spawn=True)
    assert "sys_advise_models" not in tool_dispatch._granted_tool_names(spec)
    set_remote_routing_available(True)
    assert "sys_advise_models" in tool_dispatch._granted_tool_names(spec)
    set_remote_routing_available(False)
    assert "sys_advise_models" not in tool_dispatch._granted_tool_names(spec)


async def test_native_override_on_os_env_spec_refuses_sys_os_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session pinned to a native harness gets no ``sys_os_*``.

    The spec declares ``claude-sdk`` with ``os_env``, which would grant them to
    an SDK session, but the session runs ``cursor-native``, whose relay offers
    none. Gating on the spec's declaration instead of the session's harness
    would grant a shell the model was never shown.
    """
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(
        spec_version=1,
        async_enabled=True,
        os_env=OSEnvSpec(),
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    direct = json.loads(
        await tool_dispatch.execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "x"}),
            agent_spec=spec,
            effective_harness="cursor-native",
        )
    )
    assert "not enabled" in direct["error"]

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    spawned = await _call_sys_call_async(
        spec, "sys_os_read", inbox=inbox, tasks=tasks, harness="cursor-native"
    )
    assert spawned.startswith("Error: sys_call_async refused:")
    assert tasks == {}
    await asyncio.sleep(0)
    assert executed == []


async def test_sdk_override_on_native_spec_refuses_sys_os_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native-declared spec running an SDK harness loses the relay allowance.

    Without ``os_env`` the SDK session never advertises ``sys_os_*``, so the
    spec's ``claude-native`` declaration must not keep granting them.
    """
    executed: list[str] = []

    async def _record_os_env(tool_name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(tool_name)
        return "should not run"

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _record_os_env)
    spec = AgentSpec(
        spec_version=1,
        async_enabled=True,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    direct = json.loads(
        await tool_dispatch.execute_tool(
            tool_name="sys_os_shell",
            arguments=json.dumps({"command": "id"}),
            agent_spec=spec,
            effective_harness="claude-sdk",
        )
    )
    assert "sys_os_shell" in direct["error"]
    assert "not enabled" in direct["error"]

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    spawned = await _call_sys_call_async(
        spec, "sys_os_shell", inbox=inbox, tasks=tasks, harness="claude-sdk"
    )
    assert spawned.startswith("Error: sys_call_async refused:")
    assert tasks == {}
    assert inbox.empty()
    await asyncio.sleep(0)
    assert executed == []


@pytest.mark.parametrize("os_env_option", ["fork", "start_in_scratch"])
async def test_surface_probe_does_not_fork_the_working_tree(
    monkeypatch: pytest.MonkeyPatch,
    os_env_option: str,
) -> None:
    """Metadata probes never construct an environment, even for fork/scratch specs."""
    from omnigent.inner import os_env as os_env_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    create_env = Mock(side_effect=AssertionError("metadata must not construct an OS environment"))
    monkeypatch.setattr(os_env_mod, "create_os_environment", create_env)
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
            fork=os_env_option == "fork",
            start_in_scratch=os_env_option == "start_in_scratch",
        ),
    )

    granted = tool_dispatch._granted_tool_names(spec)
    schemas = tool_dispatch.build_native_relay_tool_schemas(spec)
    request_schemas = tool_dispatch.ToolManager(spec, os_env_schema_only=True).get_tool_schemas()

    assert granted >= tool_dispatch._OS_ENV_TOOLS
    assert {schema["name"] for schema in schemas}.isdisjoint(tool_dispatch._OS_ENV_TOOLS)
    assert {
        schema["function"]["name"] for schema in request_schemas
    } >= tool_dispatch._OS_ENV_TOOLS
    create_env.assert_not_called()
