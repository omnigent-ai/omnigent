"""Built-in tool: read a file from a skill's directory."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from omnigent.spec.types import SkillSpec
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments


class ReadSkillFileTool(Tool):
    """
    Built-in tool that reads a file from a skill's directory.

    Requires both a ``skill_name`` and a ``path`` argument so
    the LLM can read resources from any skill without needing
    a global "active skill" state.

    :param skills: The agent's parsed skill list (only those
        with bundled resource files are relevant).
    """

    def __init__(self, skills: list[SkillSpec]) -> None:
        """
        Initialize with the agent's skill list.

        :param skills: Parsed skills from the agent spec.
        """
        self._skills_by_name: dict[str, SkillSpec] = {s.name: s for s in skills}

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"read_skill_file"``.
        """
        return "read_skill_file"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Read a file from a skill's directory "
            "(references/, scripts/, or assets/). "
            "Requires the skill name and a relative "
            "file path."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format schema for ``read_skill_file``.

        :returns: A tool schema dict.
        """
        return {
            "type": "function",
            "function": {
                "name": "read_skill_file",
                "description": (
                    "Read a file from a skill's directory "
                    "(references/, scripts/, or assets/). "
                    "Requires the skill name and a relative "
                    "file path."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": ("Name of the skill whose file to read"),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Relative path within the "
                                "skill directory, e.g. "
                                "'references/style-guide.md'"
                            ),
                        },
                    },
                    "required": ["skill_name", "path"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Read a file from the named skill's directory.

        Validates that the path is relative and contained
        within the skill directory (no traversal).

        :param arguments: JSON with ``"skill_name"`` and
            ``"path"`` keys, e.g.
            ``'{"skill_name": "code-review",
            "path": "references/style-guide.md"}'``.
        :param ctx: Server-side execution context (unused by
            skill tools, required by the :class:`Tool` interface).
        :returns: The file contents, or an error message.
        """
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return f"Error: {error}"
        assert args is not None

        skill_name = args.get("skill_name")
        if skill_name is None or skill_name == "":
            return "Error: missing required 'skill_name' argument"
        if not isinstance(skill_name, str):
            return "Error: 'skill_name' must be a string"
        rel_path = args.get("path")
        if rel_path is None or rel_path == "":
            return "Error: missing required 'path' argument"
        if not isinstance(rel_path, str):
            return "Error: 'path' must be a string"

        skill = self._skills_by_name.get(skill_name)
        if skill is None:
            available = list(self._skills_by_name.keys())
            return f"Error: skill {skill_name!r} not found. Available skills: {available}"
        if skill.skill_dir is None:
            return "Error: skill has no directory on disk (loaded from in-memory config)."
        return _read_file_safely(skill.skill_dir, rel_path)


def _read_file_safely(
    skill_dir: Path,
    rel_path: str,
) -> str:
    """
    Safely read a file relative to a skill directory.

    Uses ``PurePosixPath`` for parsing and
    ``Path.is_relative_to()`` for containment to prevent
    directory traversal attacks.

    :param skill_dir: Absolute path to the skill directory,
        e.g. ``Path("/agents/code-review")``.
    :param rel_path: Relative path within the skill
        directory, e.g. ``"references/style-guide.md"``.
    :returns: The file contents as a string, or an error
        message if the path is invalid or the file does
        not exist.
    """
    parsed = PurePosixPath(rel_path)
    if parsed.is_absolute():
        return "Error: path must be relative"

    resolved = (skill_dir / rel_path).resolve()
    if not resolved.is_relative_to(skill_dir.resolve()):
        return "Error: path traversal not allowed"
    if not resolved.is_file():
        return f"Error: file not found: {rel_path}"

    return resolved.read_text()
