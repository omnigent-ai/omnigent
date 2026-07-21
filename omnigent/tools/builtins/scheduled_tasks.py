"""Built-in tools for managing scheduled tasks (recurring agent runs).

A scheduled task fires an agent session on a recurring RRULE schedule. These
tools let an agent create, list, update, and delete its own scheduled tasks. The
runner dispatches each to the Omnigent server's ``/v1/scheduled-tasks`` REST
endpoints (same posture as the policy / session-query tools) — the runner has no
in-process store.

* ``sys_scheduled_task_create`` — create a recurring task.
* ``sys_scheduled_task_list`` — list the caller's tasks.
* ``sys_scheduled_task_update`` — update a task's mutable fields.
* ``sys_scheduled_task_delete`` — delete a task.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

_RRULE_DESC = (
    "RFC 5545 recurrence rule, e.g. 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0' (daily at "
    "9am) or 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0' (weekday "
    "mornings). Must fire at least twice and no more often than once per hour."
)


class SysScheduledTaskCreateTool(Tool):
    """Create a scheduled task. Runner-dispatched to ``POST /v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_create"``."""
        return "sys_scheduled_task_create"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Create a scheduled task: a saved prompt that runs an agent session "
            "on a recurring schedule (RRULE). Provide the agent to run, the "
            "prompt to send it, and the recurrence rule. The workspace is always "
            "optional and defaults to the launch host's home directory (fine for "
            "MCP-only / chat tasks that touch no code directory). Optionally PIN "
            "a connected host and/or a workspace on it; with no pinned host it "
            "runs on your live host at fire time (the owner must have an online "
            "host then, else the run is recorded as failed). The task fires "
            "automatically on its schedule until deleted."
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
                        "name": {
                            "type": "string",
                            "description": "Human-readable task name, e.g. 'nightly triage'.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "The instruction dispatched to the agent on each firing."
                            ),
                        },
                        "rrule": {"type": "string", "description": _RRULE_DESC},
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "The agent to run, e.g. 'ag_abc123' — from "
                                "sys_agent_list or sys_agent_get."
                            ),
                        },
                        "timezone": {
                            "type": "string",
                            "description": (
                                "IANA timezone the rule is evaluated in, e.g. "
                                "'America/Los_Angeles'. Defaults to 'UTC'."
                            ),
                        },
                        "model_override": {
                            "type": "string",
                            "description": (
                                "Optional per-run model override. Omit for the agent default."
                            ),
                        },
                        "reasoning_effort": {
                            "type": "string",
                            "description": "Optional per-run reasoning-effort hint, e.g. 'high'.",
                        },
                        "workspace": {
                            "type": "string",
                            "description": (
                                "Optional existing absolute path where the run's runner "
                                "starts. Omit to default to the launch host's home "
                                "directory (whether the host is pinned or resolved)."
                            ),
                        },
                        "host_id": {
                            "type": "string",
                            "description": (
                                "Optional PIN of a connected host to run on, from the "
                                "current workspace's host list. Omit to run on the owner's "
                                "online host at fire time; a failed run is recorded if none "
                                "is online."
                            ),
                        },
                    },
                    "required": ["name", "prompt", "rrule", "agent_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysScheduledTaskListTool(Tool):
    """List the caller's scheduled tasks. Dispatched to ``GET /v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_list"``."""
        return "sys_scheduled_task_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "List your scheduled tasks (recurring agent runs) with their schedules and state."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


class SysScheduledTaskUpdateTool(Tool):
    """Update a scheduled task. Dispatched to ``PATCH /v1/scheduled-tasks/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_update"``."""
        return "sys_scheduled_task_update"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Update a scheduled task's mutable fields. Only the fields you pass "
            "change; omit the rest. Pass state='paused' to stop it firing "
            "without deleting it, or state='active' to resume."
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
                        "scheduled_task_id": {
                            "type": "string",
                            "description": "The task to update (from sys_scheduled_task_list).",
                        },
                        "name": {"type": "string", "description": "New task name."},
                        "prompt": {"type": "string", "description": "New prompt."},
                        "rrule": {"type": "string", "description": _RRULE_DESC},
                        "timezone": {"type": "string", "description": "New IANA timezone."},
                        "model_override": {"type": "string", "description": "New model override."},
                        "reasoning_effort": {
                            "type": "string",
                            "description": "New reasoning-effort hint.",
                        },
                        "workspace": {
                            "type": "string",
                            "description": "New existing absolute runner start path.",
                        },
                        "host_id": {
                            "type": "string",
                            "description": "New connected host to run on.",
                        },
                        "state": {
                            "type": "string",
                            "description": "'active' or 'paused'.",
                        },
                    },
                    "required": ["scheduled_task_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysScheduledTaskDeleteTool(Tool):
    """Delete a scheduled task. Dispatched to ``DELETE /v1/scheduled-tasks/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_scheduled_task_delete"``."""
        return "sys_scheduled_task_delete"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Delete a scheduled task so it no longer fires."

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
                        "scheduled_task_id": {
                            "type": "string",
                            "description": "The task to delete (from sys_scheduled_task_list).",
                        },
                    },
                    "required": ["scheduled_task_id"],
                    "additionalProperties": False,
                },
            },
        }
