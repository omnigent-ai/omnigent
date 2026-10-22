"""Skill tools reach native harnesses through the relay.

A native session ignores ``request.tools`` and sees only
``build_native_relay_tool_schemas``. ``load_skill`` is what discovers
host-scope skills — ``.agents/skills``, ``.claude/skills`` and their
home-directory equivalents — so if it is absent from the relay, a skill dropped
where the docs say every agent picks it up is invisible to every native
harness, silently.

The per-family sources do not cover it: codex walks its bundle plus
``~/.codex/skills``, cursor walks ``~/.cursor/skills``, and the agy provider
skips the generic walk entirely.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.runner.tool_dispatch import (
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _SKILL_TOOLS,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import AgentSpec


def test_skill_tools_are_in_the_native_relay_union() -> None:
    """Without this membership the relay filters skill schemas out."""
    assert _SKILL_TOOLS <= _NATIVE_RELAY_BUILTIN_TOOLS


def test_load_skill_reaches_a_bare_spec() -> None:
    """A spec declaring no skills of its own still gets ``load_skill``.

    That is the point rather than an oversight: ``ToolManager`` registers
    ``load_skill`` unconditionally *because* its discovery covers host-scope
    directories, which an agent does not declare. Gating it on bundled skills
    would leave exactly the documented case — drop a folder in
    ``~/.agents/skills`` and every agent picks it up — still broken.
    """
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1))

    names = {schema["name"] for schema in schemas}
    assert "load_skill" in names


def test_relayed_skill_schemas_are_the_flat_shape() -> None:
    """The bridges consume ``{name, description, parameters}`` directly.

    A schema that arrives nested or without parameters is one the harness
    cannot register, which fails at spawn rather than at call time.

    Pinned on ``load_skill`` alone: ``ToolManager`` registers that one
    unconditionally, while ``read_skill_file`` is meant to appear only once a
    skill actually has resources to read.
    """
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1))
    relayed = {s["name"]: s for s in schemas if s["name"] in _SKILL_TOOLS}

    assert "load_skill" in relayed
    for schema in relayed.values():
        assert schema["description"]
        parameters = schema["parameters"]
        assert isinstance(parameters, dict)
        assert parameters["type"] == "object"


def test_relayed_load_skill_discovers_a_host_scope_skill(tmp_path: Path) -> None:
    """The relayed tool reaches the directories the docs advertise.

    Membership in the union is only half the claim; the other half is that
    ``load_skill`` run from a native session's workspace actually finds a skill
    dropped in ``.claude/skills``, which is what makes relaying it worth doing.
    """
    from omnigent.runner.tool_dispatch import _execute_skill_tool

    skill_dir = tmp_path / ".claude" / "skills" / "host-only"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: host-only\ndescription: a host-scope skill\n---\n\nthe skill body\n"
    )

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "host-only"},
        agent_spec=None,
        runner_workspace=tmp_path,
    )

    assert "the skill body" in loaded, loaded[:300]
