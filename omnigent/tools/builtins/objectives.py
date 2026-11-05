"""Tools an agent uses to record what it set out to do and how it went.

* ``sys_record_objective`` records an objective; it becomes the session's latest.
* ``sys_record_outcome`` records whether the agent reached an objective, with
  an emoji and a few words.

Both are schema-only: the runner sends them to the server's
``/v1/sessions/{id}/objectives`` routes. They register when the agent spec
enables ``tool_groups.objectives``, which by default the runner does only in
sessions an automation serves.
"""

from __future__ import annotations

from typing import Any

from omnigent.entities.session_objective import (
    OBJECTIVE_TEXT_MAX_CHARS,
    OUTCOME_EMOJI_MAX_CHARS,
    OUTCOME_TEXT_MAX_CHARS,
)
from omnigent.tools.base import Tool


class SysRecordObjectiveTool(Tool):
    """Record an objective for the current session."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_record_objective"``."""
        return "sys_record_objective"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Record what you are setting out to do in this session, in one line. Call it "
            "when you start on a goal, and again whenever the goal changes; each call "
            "adds an objective, and the latest one is current. Returns the objective's "
            "id. When you finish, or give up on it, call sys_record_outcome. The user "
            "sees your objectives and outcomes in order, like a log of what you tried."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "objective": {
                            "type": "string",
                            "maxLength": OBJECTIVE_TEXT_MAX_CHARS,
                            "description": (
                                "The goal, stated so its outcome can be judged, e.g. "
                                "'Get the nightly build green' or 'Triage new issues'."
                            ),
                        },
                    },
                    "required": ["objective"],
                    "additionalProperties": False,
                },
            },
        }


class SysRecordOutcomeTool(Tool):
    """Record how an objective went."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_record_outcome"``."""
        return "sys_record_outcome"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Record whether you reached an objective, before your final reply on it. "
            "Without objective_id it resolves your latest objective (in an automation "
            "run, the latest one of this run), or records the outcome on its own when "
            "there is none. Calling it again for the same objective replaces the "
            "outcome. Judge success by the objective; finishing your turn is not success "
            "by itself, and doing only part of it is not success. The user sees the "
            "emoji and text, like a status message."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "success": {
                            "type": "boolean",
                            "description": (
                                "true if you reached the objective, false if you did not "
                                "(including when you were blocked or did only part of it)."
                            ),
                        },
                        "emoji": {
                            "type": "string",
                            "maxLength": OUTCOME_EMOJI_MAX_CHARS,
                            "description": (
                                "One emoji summarizing the outcome, e.g. '🚀', '🧹', '🚧'."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "maxLength": OUTCOME_TEXT_MAX_CHARS,
                            "description": (
                                "A few words on the outcome, e.g. 'Opened 2 PRs' or "
                                "'Blocked: CI is down'."
                            ),
                        },
                        "objective_id": {
                            "type": "string",
                            "description": (
                                "The id sys_record_objective returned. Omit it to resolve "
                                "your latest objective."
                            ),
                        },
                    },
                    "required": ["success", "emoji", "text"],
                    "additionalProperties": False,
                },
            },
        }
