"""Framework-owned tool for renaming the current session."""

from __future__ import annotations

import os
from typing import Any

from omnigent.tools.base import Tool

CLAUDE_NATIVE_SESSION_RENAME_TOOL = "mcp__omnigent__sys_session_rename"

# Automatic, agent-driven session rename ("auto-title") runs on the first turn
# of every fresh session: the model is asked to call ``sys_session_rename`` with
# a short summary title. That is an extra model round-trip, so it ships DISABLED
# by default. Set ``OMNIGENT_SESSION_RENAME=on`` (``1``/``true``/``yes`` also
# work) to opt back in. This gates only the *automatic* rename — the manual
# "Rename" item in the web sidebar is unaffected. ``session_rename_instruction``
# and ``session_rename_allowed_tools`` are the single gate both the Claude-native
# launcher (``omnigent/claude_native.py``) and the shared runner
# (``omnigent/runner/app.py``) consult, so suppressing the instruction and
# emptying the preapproval here disables the feature everywhere while keeping
# the implementation (tool, endpoint, dispatch) in place.
_SESSION_RENAME_TRUTHY = ("1", "true", "yes", "on")


def _auto_rename_enabled() -> bool:
    """Return whether the automatic first-turn session rename is enabled.

    Defaults to off; opt in via the ``OMNIGENT_SESSION_RENAME`` env var. Read on
    every call rather than cached at import so tests can flip it with
    ``monkeypatch.setenv`` and deployments pick up changes without a reimport;
    the helpers run once per session, so the ``os.environ`` lookup is negligible.
    """
    return os.environ.get("OMNIGENT_SESSION_RENAME", "").strip().lower() in _SESSION_RENAME_TRUTHY


SESSION_RENAME_INSTRUCTION = """
Omnigent creates each session with its title set to the user's full prompt verbatim. On the
FIRST turn, before doing any other work or replying, call sys_session_rename with a short
summary-style title (3-6 words, ≤60 characters, action-first). Strip filler; keep the noun + verb.
Summarize the user's actual intent; do not copy a conversational prompt verbatim or use generic
titles such as "Help with task", "Create new design", or "Answer question".

  prompt: "Could you please help me figure out why my React app is re-rendering twice on
           every state change?"
  title:  "Debug double React re-render"

  prompt: "What should we work on today?"
  title:  "Plan today's priorities"

Every fresh session must call sys_session_rename, including when the prompt is short or already
resembles a finished title. Questions, greetings, brainstorming openers, and requests for help
must also be renamed. Resumed sessions skip it. If your harness defers tools, load
sys_session_rename with its tool-discovery mechanism first. In Claude Code, use ToolSearch with
the exact query
select:mcp__omnigent__sys_session_rename; if it reports that the omnigent server is still
connecting, repeat that exact search rather than switching to a semantic query or giving up.
In Claude SDK, invoke mcp__omnigent__sys_session_rename directly. The call is silent; the user
only sees the title change. If the tool is unavailable after the server finishes connecting,
declines the rename, or returns an error, continue the user's turn normally.
""".strip()


def session_rename_allowed_tools(*, initial_session: bool) -> tuple[str, ...]:
    """Return native Claude tools preapproved for automatic session metadata.

    Returns an empty tuple when automatic rename is disabled (the default; see
    ``OMNIGENT_SESSION_RENAME``), so the rename tool is never preapproved.

    :param initial_session: Whether this is the session's initial model context.
    :returns: A scoped allowlist containing only the rename tool for fresh sessions.
    """
    if not _auto_rename_enabled():
        return ()
    return (CLAUDE_NATIVE_SESSION_RENAME_TOOL,) if initial_session else ()


def session_rename_instruction(*, initial_session: bool) -> str | None:
    """Return the rename directive when the caller identifies an initial session.

    The shared runner derives ``initial_session`` from persisted message history.
    Native launchers derive it from the absence of a resumed external session or
    carried fork history. Keeping the selection here gives both layers one
    canonical gate while allowing each to use the state it owns. Returns
    ``None`` (no instruction injected) when automatic rename is disabled — the
    default; set ``OMNIGENT_SESSION_RENAME=on`` to opt in.

    :param initial_session: Whether this is the session's initial model context.
    :returns: The rename instruction for an initial session, otherwise ``None``.
    """
    if not _auto_rename_enabled():
        return None
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
            "Never copy a conversational question or greeting verbatim. "
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
