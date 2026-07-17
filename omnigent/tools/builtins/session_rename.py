"""Framework-owned tool for renaming the current session."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

SESSION_RENAME_INSTRUCTION = """
Omnigent creates each session with its title set to the user's full prompt verbatim. On the
FIRST turn, call sys_session_rename with a short summary-style title (3-6 words, ≤60
characters, action-first). Strip filler; keep the noun + verb.

  prompt: "Could you please help me figure out why my React app is re-rendering twice on
           every state change?"
  title:  "Debug double React re-render"

Skip sys_session_rename only if the prompt is already a short, clean title. Resumed sessions
skip it too. If your harness defers tools, load sys_session_rename with its tool-discovery
mechanism first (for Codex, use ToolSearch). The call is silent; the user only sees the title
change. If the tool is unavailable after discovery or declines the rename, continue normally.
""".strip()


def session_rename_instruction(*, initial_session: bool) -> str | None:
    """Return the rename directive when the caller identifies an initial session.

    The shared runner derives ``initial_session`` from persisted message history.
    Native launchers derive it from the absence of a resumed external session or
    carried fork history. Keeping the selection here gives both layers one
    canonical gate while allowing each to use the state it owns.

    :param initial_session: Whether this is the session's initial model context.
    :returns: The rename instruction for an initial session, otherwise ``None``.
    """
    return SESSION_RENAME_INSTRUCTION if initial_session else None


class SysSessionRenameTool(Tool):
    """Schema-only tool that renames the calling session."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_rename"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Rename the current top-level session with a short summary-style title "
            "(3-6 words, action-first). Strip filler and keep the noun plus verb. "
            "This is silent framework startup metadata; the rename is ignored if the "
            "title changed."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                "Short summary-style, action-first session title, for "
                                "example 'Debug authentication timeout'."
                            ),
                            "minLength": 2,
                            "maxLength": 60,
                        }
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
        }
