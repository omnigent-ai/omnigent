"""The ``thingy`` framework skill: what the short sys_thingy_* descriptions point agents to."""

from __future__ import annotations

import json
import re

from omnigent.automations_availability import without_thingy_tools
from omnigent.spec.skill_sources import FRAMEWORK_SKILLS_DIR, framework_skills
from omnigent.spec.types import AgentSpec, ToolGroupsConfig
from omnigent.tools.builtins.thingies import THINGY_TOOL_NAMES, THINGY_TOOLS

SKILL = FRAMEWORK_SKILLS_DIR / "thingy" / "SKILL.md"


def _skills(spec: AgentSpec, *, workflows: bool) -> set[str]:
    return {skill.name for skill in framework_skills(spec, workflows=workflows)}


def test_the_skill_comes_with_the_thingy_tool_group_not_with_workflows() -> None:
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(journal=False))
    assert _skills(spec, workflows=False) == {"thingy"}
    assert _skills(AgentSpec(spec_version=1), workflows=True) == {
        "thingy",
        "workflow",
        "save-workflow",
    }
    assert "thingy" not in _skills(without_thingy_tools(spec), workflows=True)
    [skill] = [s for s in framework_skills(spec, workflows=False) if s.name == "thingy"]
    # Agent-facing: it never shows in the composer's slash menu.
    assert skill.user_invocable is False
    assert "Thingy action" in skill.description


def test_the_skill_names_only_tools_that_exist_and_the_exact_watch_signatures() -> None:
    text = SKILL.read_text()
    named = set(re.findall(r"sys_thingy_[a-z_]+", text))
    assert named <= THINGY_TOOL_NAMES, named - THINGY_TOOL_NAMES
    assert {"sys_thingy_describe", "sys_thingy_action_update", "sys_thingy_records"} <= named
    # The listener comes first, the action IDs go in options, and snapshots list items.
    assert "watch(listener: (snapshot: {" in text
    assert "options?: { actionIds?: string[]; intervalMs?: number }" in text
    assert "{ actionIds: [actionId] }" in text
    assert "snapshot.items.find((item) => item.id === actionId)" in text


def test_the_tool_schemas_stay_small_and_point_to_the_skill() -> None:
    schemas = {tool.name(): tool().get_schema() for tool in THINGY_TOOLS}
    # Every turn carries these; the how-to belongs in the skill.
    assert sum(len(json.dumps(schema)) for schema in schemas.values()) < 7500
    for name in (
        "sys_thingy_create",
        "sys_thingy_revision_create",
        "sys_thingy_publish",
        "sys_thingy_action_update",
        "sys_thingy_records",
    ):
        assert "Load the thingy skill" in schemas[name]["function"]["description"], name


def test_the_skill_documents_editing_a_revision_instead_of_resending_it() -> None:
    text = SKILL.read_text()
    assert "`base_revision_id` and `edits`" in text
    assert "`{old, new}`" in text and "`{start, end, new}`" in text
