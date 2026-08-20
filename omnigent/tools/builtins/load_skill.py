"""Built-in tool: load a skill's instructions by name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnigent.spec.types import SkillSpec
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments


class LoadSkillTool(Tool):
    """
    Built-in tool that loads a skill's full instructions by name.

    Looks up the skill from bundled skills (in the agent spec)
    and host-scope skills (``.claude/skills/``, ``.agents/skills/``,
    ``~/.claude/skills/``, ``~/.agents/skills/``). Returns the
    skill content with an optional resource file listing appended.

    :param skills: The agent's bundled skill list.
    :param agent_root: Path to the agent's working directory,
        used to discover host-scope skills. ``None`` skips
        host-scope discovery.
    :param skills_filter: The agent spec's ``skills_filter``
        value (``"all"``/``"none"``/list). Controls which
        host-scope skills are included.
    """

    def __init__(
        self,
        skills: list[SkillSpec],
        agent_root: Path | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        """
        Initialize with bundled + host-scope skills.

        :param skills: Parsed skills from the agent spec.
        :param agent_root: Agent working directory for host
            skill discovery.
        :param skills_filter: Host-scope skill filter from
            the agent spec.
        """
        all_skills = list(skills)
        # Discover host-scope skills. Use agent_root when provided,
        # but fall back to cwd — in production the server process
        # runs from the user's project, so cwd finds .claude/skills/
        # even when agent_root is a cache dir.
        discovery_root = agent_root or Path.cwd()
        from omnigent.spec.parser import discover_host_skills

        bundled_names = {s.name for s in skills}
        for hs in discover_host_skills(discovery_root, skills_filter):
            if hs.name not in bundled_names:
                all_skills.append(hs)
        self._skills = all_skills
        self._skills_by_name: dict[str, SkillSpec] = {s.name: s for s in all_skills}

    @property
    def skills(self) -> list[SkillSpec]:
        """All discovered skills (bundled + host-scope)."""
        return self._skills

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"load_skill"``.
        """
        return "load_skill"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return "Load a skill's full instructions by name."

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format schema for ``load_skill``.

        The description includes the list of available skill
        names so the LLM knows what it can load.

        :returns: A tool schema dict.
        """
        skill_names = [s.name for s in self._skills]
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": (
                    "Load a skill's full instructions by "
                    "name. Available skills: "
                    f"{', '.join(skill_names)}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": ("The skill name to load"),
                        },
                    },
                    "required": ["name"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Look up a skill by name and return its content.

        If the skill has bundled resource files, appends
        a listing of available files to the content.

        :param arguments: JSON with ``"name"`` key, e.g.
            ``'{"name": "code-review"}'``.
        :param ctx: Server-side execution context (unused by
            skill tools, required by the :class:`Tool` interface).
        :returns: The skill content string, or an error
            message if the skill is not found.
        """
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return f"Error: {error}"
        assert args is not None

        skill_name = args.get("name")
        if skill_name is None or skill_name == "":
            return "Error: missing required 'name' argument"
        if not isinstance(skill_name, str):
            return "Error: 'name' must be a string"
        skill = self._skills_by_name.get(skill_name)
        if skill is None:
            available = list(self._skills_by_name.keys())
            return f"Error: skill {skill_name!r} not found. Available skills: {available}"
        resources = list_skill_resources(skill)
        return format_skill_content(skill, resources)


def list_skill_resources(skill: SkillSpec) -> list[str]:
    """
    List resource files in a skill's directory.

    Scans ``references/``, ``scripts/``, and ``assets/``
    subdirectories. Returns relative paths suitable for
    ``read_skill_file``.

    :param skill: The skill to scan.
    :returns: Sorted list of relative path strings, e.g.
        ``["references/style-guide.md"]``. Empty if the
        skill has no ``skill_dir`` or no resource files.
    """
    if skill.skill_dir is None:
        return []
    files: list[str] = []
    for subdir_name in ("references", "scripts", "assets"):
        subdir = skill.skill_dir / subdir_name
        if not subdir.is_dir():
            continue
        for fp in sorted(subdir.rglob("*")):
            if fp.is_file():
                rel = str(fp.relative_to(skill.skill_dir))
                files.append(rel)
    return files


def format_skill_content(
    skill: SkillSpec,
    resource_files: list[str],
) -> str:
    """
    Format the skill content for the LLM, appending a
    resource listing if the skill has bundled files.

    :param skill: The skill to format.
    :param resource_files: List of relative paths to
        bundled resource files, e.g.
        ``["references/style-guide.md"]``.
    :returns: The skill content, optionally followed by
        an ``## Available files`` section.
    """
    if not resource_files:
        return skill.content

    lines = [
        skill.content,
        "",
        "## Available files",
        "Use the read_skill_file tool to read these:",
    ]
    for path in resource_files:
        lines.append(f"- {path}")
    return "\n".join(lines)


def find_skill_by_name(skills: list[SkillSpec], name: str) -> SkillSpec | None:
    """
    Return the skill with the requested name.

    :param skills: Discovered skills for an agent (bundled + host),
        e.g. the merged list from :attr:`LoadSkillTool.skills`.
    :param name: Skill name to match exactly, e.g. ``"code-review"``.
    :returns: The matching :class:`SkillSpec`, or ``None`` when the
        command references a skill the agent does not expose.
    """
    for skill in skills:
        if skill.name == name:
            return skill
    return None


def format_skill_meta_text(skill: SkillSpec, arguments: str) -> str:
    """
    Build the hidden user-message text injected for a skill invocation.

    The format follows Codex's durable skill wrapper: the complete
    skill content is enclosed in ``<skill>`` so clients can identify
    it later, and slash-command arguments are appended in a separate
    ``<user_request>`` block so the agent sees the actual request.

    The embedded ``<path>`` and the resource listing are resolved
    against ``skill.skill_dir``, so this MUST run on the host where the
    harness executes (the runner) — the paths are read at runtime by
    the ``read_skill_file`` tool, which resolves relative to the same
    ``skill_dir``.

    :param skill: Skill being invoked, e.g. ``SkillSpec(name="grill-me",
        ...)``.
    :param arguments: Raw arguments typed after the slash command,
        e.g. ``"review this plan"``. Empty string when none.
    :returns: Hidden message text for a single ``input_text`` block.
    """
    resource_files = list_skill_resources(skill)
    content = format_skill_content(skill, resource_files)
    lines = ["<skill>", f"<name>{skill.name}</name>"]
    if skill.skill_dir is not None:
        lines.append(f"<path>{skill.skill_dir / 'SKILL.md'}</path>")
    lines.extend([content, "</skill>"])
    if arguments:
        lines.extend(["", "<user_request>", arguments, "</user_request>"])
    return "\n".join(lines)
