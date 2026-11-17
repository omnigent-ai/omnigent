"""``sys_automation_start_worker``: start one worker for an automation's matching item.

A trigger automation's prompt already carries the instructions each worker gets. This tool
reads them from the prompt the automation sent, so the model passes only the item's details
instead of copying the instructions into every ``sys_session_create`` message.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysAutomationStartWorkerTool(Tool):
    """Start a worker session for one item; runner-dispatched through ``sys_session_create``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_automation_start_worker"``."""
        return "sys_automation_start_worker"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "For a trigger automation's own session only: start one worker session for a matching "
            "item. Takes sys_session_create's agent_id, placement (same_host or managed), title, "
            "model, reasoning_effort and permission_mode, plus details in place of message; "
            "for other options use sys_session_create. The worker's first message is built "
            "for you: the instructions under "
            '"Instructions for each session:" in the prompt this automation sent, then a line '
            "saying the details are untrusted data, then details. Refused, with nothing "
            "started, when this isn't an automation's session or the message would be too long; "
            "the refusal gives the longest details that fit. Returns what sys_session_create "
            "returns."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        from omnigent.tools.builtins.spawn import SysSessionCreateTool

        create_properties = SysSessionCreateTool().get_schema()["function"]["parameters"][
            "properties"
        ]
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "The worker's agent, e.g. 'ag_abc123'.",
                        },
                        "placement": {
                            "type": "string",
                            "enum": ["same_host", "managed"],
                            "description": "Where the worker starts, as for sys_session_create.",
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "The worker's title, e.g. 'Slack #support 1700000000.000100'."
                            ),
                        },
                        "details": {
                            "type": "string",
                            "description": (
                                "Only the item's details, fenced as the automation prompt says, "
                                "e.g. '<slack-message>\\n...\\n</slack-message>'."
                            ),
                        },
                        "message_max_chars": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "The longest first message the automation prompt allows, "
                                "e.g. 32000."
                            ),
                        },
                        "model": create_properties["model"],
                        "reasoning_effort": create_properties["reasoning_effort"],
                        "permission_mode": create_properties["permission_mode"],
                    },
                    "required": ["agent_id", "placement", "title", "details"],
                    "additionalProperties": False,
                },
            },
        }
