"""``tool_groups:`` on the runner side: the native relay and the dispatch gate.

Native harnesses see only the relay surface, and every other harness is held
to the spec's registered surface at dispatch, so a disabled group must vanish
from both — not just from ``ToolManager``.
"""

from __future__ import annotations

from omnigent.runner.tool_dispatch import (
    _ungranted_tool_reason,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import MEMORY_TOOL_NAMES, AgentSpec, ToolGroupsConfig
from omnigent.tools.manager import ToolManager

_ALL_OFF = ToolGroupsConfig(
    browser=False,
    scheduled_tasks=False,
    comments=False,
    policies=False,
    agent_discovery=False,
    memory=False,
    files=False,
)
_ALL_ON = ToolGroupsConfig(files=True)
_FILE_TOOL_NAMES = frozenset({"sys_files_upload", "sys_files_list", "sys_files_download"})


def _spec(groups: ToolGroupsConfig) -> AgentSpec:
    """A minimal spec with the given tool groups."""
    return AgentSpec(spec_version=1, skills_filter="none", tool_groups=groups)


def _registered(spec: AgentSpec) -> set[str]:
    """Names ``ToolManager`` registers for *spec*."""
    return {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}


def _gated() -> frozenset[str]:
    """Tools the seven groups control, derived from registration itself."""
    gated = frozenset(_registered(_spec(_ALL_ON)) - _registered(_spec(_ALL_OFF)))
    assert len(gated) == 35, sorted(gated)
    return gated


def _relay_names(spec: AgentSpec) -> set[str]:
    """Names the native relay advertises for *spec*."""
    return {schema["name"] for schema in build_native_relay_tool_schemas(spec)}


def test_native_relay_carries_every_group_but_files_by_default() -> None:
    """
    Without a ``tool_groups:`` block the relay advertises every gated tool but
    the file tools, which an agent turns on with ``files: true``.
    """
    assert _gated() >= MEMORY_TOOL_NAMES | _FILE_TOOL_NAMES
    default_names = _relay_names(_spec(ToolGroupsConfig()))
    missing = (_gated() - _FILE_TOOL_NAMES) - default_names
    assert not missing, f"relay dropped {sorted(missing)} by default"
    assert not (default_names & _FILE_TOOL_NAMES), sorted(default_names & _FILE_TOOL_NAMES)
    assert _relay_names(_spec(_ALL_ON)) >= _FILE_TOOL_NAMES


def test_native_relay_drops_disabled_groups() -> None:
    """
    With every group off the relay advertises none of the gated tools but
    keeps the keyless session reads and ``sys_cancel_task``.
    """
    names = _relay_names(_spec(_ALL_OFF))
    assert not (_gated() & names), f"relay still advertises {sorted(_gated() & names)}"
    assert {"sys_session_list", "sys_session_get_info", "sys_cancel_task"} <= names


def test_dispatch_refuses_a_disabled_group_tool() -> None:
    """A model that calls a disabled tool anyway is refused, not executed."""
    enabled_spec = _spec(_ALL_ON)
    trimmed_spec = _spec(_ALL_OFF)
    for name in sorted(_gated()):
        assert _ungranted_tool_reason(name, enabled_spec) is None, name
        reason = _ungranted_tool_reason(name, trimmed_spec)
        assert reason is not None and "not enabled for this agent" in reason, name
    default_spec = _spec(ToolGroupsConfig())
    for name in sorted(_FILE_TOOL_NAMES):
        reason = _ungranted_tool_reason(name, default_spec)
        assert reason is not None and "not enabled for this agent" in reason, name


def test_spec_less_relay_and_dispatch_leave_the_file_tools_off() -> None:
    """
    When the runner can't resolve a session's spec it can't see a
    ``files: true`` opt-in, so the relay leaves the file tools out and dispatch
    refuses them, while the always-on session reads still work.
    """
    spec_less = {schema["name"] for schema in build_native_relay_tool_schemas(None)}
    assert not (spec_less & _FILE_TOOL_NAMES), sorted(spec_less & _FILE_TOOL_NAMES)
    assert {"sys_session_list", "sys_session_get_info"} <= spec_less
    for name in sorted(_FILE_TOOL_NAMES):
        reason = _ungranted_tool_reason(name, None)
        assert reason is not None and "is not enabled" in reason, name
    assert _ungranted_tool_reason("sys_session_get_info", None) is None
