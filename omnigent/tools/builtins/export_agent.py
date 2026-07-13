"""Built-in tool: export a generated agent from the sandbox workspace.

Used by the onboarding assistant to copy a completed agent directory
from the per-conversation sandbox workspace to a user-specified
location on disk.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# Any: the OpenAI tool schema is a heterogeneous dict with string
# keys and mixed value types (str, dict, list).
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins.upload_file import safe_resolve

_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "export_agent",
        "description": (
            "Copy a generated agent directory from the sandbox workspace "
            "to a target path on the user's filesystem. Use this after "
            "creating an agent with sys_os_shell to place it where the "
            "user wants it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        "Path to the agent directory inside the workspace, "
                        "relative to the workspace root. "
                        "Example: 'my-research-agent'"
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Absolute path where the agent directory should be "
                        "copied to on the user's filesystem. "
                        "Example: '/home/user/my-research-agent'"
                    ),
                },
            },
            "required": ["source", "target"],
        },
    },
}


class ExportAgentTool(Tool):
    """
    Copy a generated agent directory from the sandbox to a target path.

    The onboarding assistant generates agent files inside the
    conversation's workspace (via ``sys_os_shell``). This tool
    copies the result to the user's chosen location.

    Security: the ``source`` is resolved with workspace containment
    so it cannot escape the sandbox, symlinks inside ``source`` are
    copied as links rather than dereferenced (so host files are never
    read out of the workspace), and an existing ``target`` is refused
    rather than deleted. The tool never recursively removes a path on
    the user's filesystem.
    """

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"export_agent"``.
        """
        return "export_agent"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Copy a generated agent directory from the sandbox workspace "
            "to a target path on the user's filesystem. Use this after "
            "creating an agent with sys_os_shell to place it where the "
            "user wants it."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema.

        :returns: The schema dict.
        """
        return _SCHEMA

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Copy a directory from the workspace to a target path.

        :param arguments: JSON with ``source`` and ``target`` keys.
        :param ctx: Execution context with ``workspace`` path.
        :returns: Success message or error string.
        """
        parsed: dict[str, Any] = json.loads(arguments) if arguments else {}
        source_rel = parsed.get("source", "")
        target_str = parsed.get("target", "")

        if not source_rel:
            return "Error: 'source' parameter is required."
        if not target_str:
            return "Error: 'target' parameter is required."

        if ctx.workspace is None:
            return "Error: no workspace available."

        # Contain ``source`` inside the workspace so a traversal path
        # (e.g. "../../etc") or an escaping symlink cannot copy host
        # files out of the sandbox.
        try:
            source = safe_resolve(source_rel, ctx.workspace)
        except ValueError:
            return f"Error: source '{source_rel}' escapes the workspace."

        target = Path(target_str)

        if not source.exists():
            return f"Error: source directory '{source_rel}' not found in workspace."

        if not source.is_dir():
            return f"Error: source '{source_rel}' is not a directory."

        # Never recursively delete an LLM-controlled target path. The
        # target lives on the user's filesystem, so refusing an
        # existing path avoids arbitrary directory deletion.
        if target.exists() or target.is_symlink():
            return (
                f"Error: target '{target}' already exists. Refusing to "
                "delete or overwrite it; choose a path that does not exist."
            )

        # symlinks=True copies links as links instead of dereferencing
        # them, so a symlink inside ``source`` cannot pull host file
        # contents into the exported copy.
        shutil.copytree(str(source), str(target), symlinks=True)
        return f"Exported agent to {target}"
