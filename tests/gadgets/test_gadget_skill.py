"""The ``gadget`` framework skill: what the short sys_gadget_* descriptions point agents to."""

from __future__ import annotations

import json
import re

from omnigent.automations_availability import without_gadget_tools
from omnigent.spec.skill_sources import FRAMEWORK_SKILLS_DIR, framework_skills
from omnigent.spec.types import AgentSpec, ToolGroupsConfig
from omnigent.tools.builtins.gadgets import GADGET_TOOL_NAMES, GADGET_TOOLS

SKILL = FRAMEWORK_SKILLS_DIR / "gadget" / "SKILL.md"


def _skills(spec: AgentSpec, *, workflows: bool) -> set[str]:
    return {skill.name for skill in framework_skills(spec, workflows_enabled=workflows)}


def test_the_skill_comes_with_the_gadget_tool_group_not_with_workflows() -> None:
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(journal=False, memory=False))
    assert _skills(spec, workflows=False) == {"gadget"}
    assert _skills(
        AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(memory=False)), workflows=True
    ) == {
        "gadget",
        "workflow",
        "save-workflow",
    }
    assert "gadget" not in _skills(without_gadget_tools(spec), workflows=True)
    [skill] = [s for s in framework_skills(spec, workflows_enabled=False) if s.name == "gadget"]
    # Agent-facing: it never shows in the composer's slash menu.
    assert skill.user_invocable is False
    assert "Gadget action" in skill.description


def test_the_skill_names_only_tools_that_exist_and_the_exact_watch_signatures() -> None:
    text = SKILL.read_text()
    named = set(re.findall(r"sys_gadget_[a-z_]+", text))
    assert named <= GADGET_TOOL_NAMES, named - GADGET_TOOL_NAMES
    assert {"sys_gadget_describe", "sys_gadget_action_update", "sys_gadget_records"} <= named
    # The listener comes first, the action IDs go in options, and snapshots list items.
    assert "watch(listener: (snapshot: {" in text
    assert "options?: { actionIds?: string[]; intervalMs?: number }" in text
    assert "{ actionIds: [actionId] }" in text
    assert "snapshot.items.find((item) => item.id === actionId)" in text


def test_the_tool_schemas_stay_small_and_point_to_the_skill() -> None:
    schemas = {tool.name(): tool().get_schema() for tool in GADGET_TOOLS}
    # Every turn carries these; the how-to belongs in the skill.
    assert sum(len(json.dumps(schema)) for schema in schemas.values()) < 7500
    for name in (
        "sys_gadget_create",
        "sys_gadget_revision_create",
        "sys_gadget_publish",
        "sys_gadget_action_update",
        "sys_gadget_records",
    ):
        assert "Load the gadget skill" in schemas[name]["function"]["description"], name
