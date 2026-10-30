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
from omnigent.spec.types import AgentSpec, ToolGroupsConfig
from omnigent.tools.manager import ToolManager

_ALL_OFF = ToolGroupsConfig(
    browser=False,
    scheduled_tasks=False,
    comments=False,
    policies=False,
    agent_discovery=False,
)


def _spec(groups: ToolGroupsConfig) -> AgentSpec:
    """A minimal spec with the given tool groups."""
    return AgentSpec(spec_version=1, skills_filter="none", tool_groups=groups)


def _registered(spec: AgentSpec) -> set[str]:
    """Names ``ToolManager`` registers for *spec*."""
    return {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}


def _gated() -> frozenset[str]:
    """Tools the five groups control, derived from registration itself."""
    gated = frozenset(_registered(_spec(ToolGroupsConfig())) - _registered(_spec(_ALL_OFF)))
    assert len(gated) == 16, sorted(gated)
    return gated


def _relay_names(spec: AgentSpec) -> set[str]:
    """Names the native relay advertises for *spec*."""
    return {schema["name"] for schema in build_native_relay_tool_schemas(spec)}


def test_native_relay_carries_every_group_by_default() -> None:
    """Without a ``tool_groups:`` block the relay advertises all 16 gated tools."""
    missing = _gated() - _relay_names(_spec(ToolGroupsConfig()))
    assert not missing, f"relay dropped {sorted(missing)} with every group on"


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
    default_spec = _spec(ToolGroupsConfig())
    trimmed_spec = _spec(_ALL_OFF)
    for name in sorted(_gated()):
        assert _ungranted_tool_reason(name, default_spec) is None, name
        reason = _ungranted_tool_reason(name, trimmed_spec)
        assert reason is not None and "not enabled for this agent" in reason, name
