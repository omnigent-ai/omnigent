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
    "mornings). Must fire at least twice. The minimum is 10 minutes on "
    "connected hosts in either session mode, or "
    "in managed sandboxes with session_mode 'continue' "
    "(e.g. 'FREQ=MINUTELY;INTERVAL=10'). New sandbox sessions, or servers "
    "without sub-hour support, require 1 hour."
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
            "prompt to send it, and the recurrence rule. The workspace is optional: "
            "a connected-host directory (defaulting to its home), or a repository URL "
            "for managed_sandbox (defaulting to the provider's repository). With "
            "connected_host, optionally pin a host and/or a workspace on it; with no "
            "pinned host it runs on your live host at fire time (the owner must have an online "
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
                        "permission_mode": {
                            "type": ["string", "null"],
                            "description": (
                                "Optional permission mode for native coding agents that "
                                "support one. Use the selected harness's vocabulary: "
                                "Claude Code (e.g. 'acceptEdits'), Codex ('default', "
                                "'ask-for-approval', 'approve-for-me', 'read-only', "
                                "'full-access', 'bypass'), "
                                "Cursor (e.g. 'auto-review'), "
                                "Antigravity ('default', 'skip'), or Devin (e.g. 'smart'). "
                                "Runs are unattended, so a prompting mode ('default'/'plan') "
                                "stalls waiting for approval — prefer an auto-running mode. "
                                "Set null for the agent default. This field is required so "
                                "the user can review the effective task setting."
                            ),
                        },
                        "max_cost_usd": {
                            "type": "number",
                            "description": (
                                "Optional per-firing cost budget in USD. When set, each "
                                "fired session is capped at this spend — all models are "
                                "blocked once the limit is reached. Omit for no cap."
                            ),
                        },
                        "session_mode": {
                            "type": "string",
                            "enum": ["new", "continue"],
                            "description": (
                                "Optional. 'new' (default) starts a new session for every "
                                "run. 'continue' sends every run's prompt into one session, "
                                "keeping its context: session_id when given, otherwise the "
                                "session the first run creates."
                            ),
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Optional, with session_mode 'continue'. An existing "
                                "top-level session the owner owns, e.g. the current session, "
                                "to send every run's prompt into. agent_id must be that "
                                "session's agent."
                            ),
                        },
                        "workspace": {
                            "type": "string",
                            "description": (
                                "For connected_host: an optional existing absolute runner "
                                "start path; omit for the host's home directory. For "
                                "managed_sandbox: an optional repository URL, optionally "
                                "with #branch; omit for the provider default."
                            ),
                        },
                        "host_id": {
                            "type": "string",
                            "description": (
                                "Optional PIN of a connected host to run on, from the "
                                "current workspace's host list. Omit to run on the owner's "
                                "online host at fire time; a failed run is recorded if none "
                                "is online. Existing managed sandbox hosts cannot be pinned."
                            ),
                        },
                        "execution_target": {
                            "type": "string",
                            "enum": ["connected_host", "managed_sandbox"],
                            "description": (
                                "Where each firing runs. 'connected_host' (default) uses a "
                                "pinned/resolved connected host. 'managed_sandbox' provisions "
                                "a fresh server-managed sandbox per firing, using the server's "
                                "sandbox lifecycle settings. Do not set host_id with it; "
                                "workspace may name a repository URL. Requires managed "
                                "sandboxes configured on the server."
                            ),
                        },
                        "project_name": {
                            "type": "string",
                            "description": (
                                "Optional project to file every fired session into, by "
                                "name: your existing project with this name, or a new one "
                                "if you have none. Do not set project_id with it."
                            ),
                        },
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Optional existing project id to file every fired session "
                                "into. Do not set project_name with it."
                            ),
                        },
                    },
                    "required": [
                        "name",
                        "prompt",
                        "rrule",
                        "agent_id",
                        "permission_mode",
                    ],
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
            "change, except prompt and permission_mode: first list the task and "
            "resend those complete review fields even when they are unchanged. "
            "Pass agent_id to switch which agent/harness "
            "it runs. Pass state='paused' to stop it firing without deleting it, "
            "or state='active' to resume. A 'completed' task has no remaining "
            "occurrences; change its rrule to schedule it again."
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
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "Rebind the task to a different agent, switching the "
                                "harness its future firings run (e.g. Cursor to Pi) — "
                                "from sys_agent_list. Past runs keep the agent they "
                                "ran. Model, effort, and permission mode do not carry "
                                "across a switch: resend any you want set."
                            ),
                        },
                        "timezone": {"type": "string", "description": "New IANA timezone."},
                        "model_override": {"type": "string", "description": "New model override."},
                        "reasoning_effort": {
                            "type": "string",
                            "description": "New reasoning-effort hint.",
                        },
                        "permission_mode": {
                            "type": ["string", "null"],
                            "description": (
                                "New permission mode using the selected harness's vocabulary: "
                                "Claude Code (e.g. 'acceptEdits'), Codex (e.g. 'read-only'), "
                                "Cursor (e.g. 'auto-review'), Antigravity ('default', 'skip'), "
                                "or Devin (e.g. 'smart'). Set null for the agent default. "
                                "Required for approval review; resend the current value "
                                "when it is unchanged. When switching to a harness "
                                "without permission-mode support, send null."
                            ),
                        },
                        "max_cost_usd": {
                            "type": "number",
                            "description": (
                                "New per-firing cost budget in USD. Null clears the cap."
                            ),
                        },
                        "session_mode": {
                            "type": "string",
                            "enum": ["new", "continue"],
                            "description": (
                                "'new' starts a new session for every run; 'continue' sends "
                                "every run's prompt into one session."
                            ),
                        },
                        "session_id": {
                            "type": ["string", "null"],
                            "description": (
                                "With session_mode 'continue': the existing session to "
                                "continue, or null so the next run starts a new session "
                                "that later runs continue."
                            ),
                        },
                        "workspace": {
                            "type": ["string", "null"],
                            "description": (
                                "For connected_host: a new existing absolute runner start "
                                "path. For managed_sandbox: a repository URL, optionally "
                                "with #branch, or null to restore the provider default. "
                                "Omit to keep the current selection."
                            ),
                        },
                        "host_id": {
                            "type": "string",
                            "description": "New connected host to run on.",
                        },
                        "execution_target": {
                            "type": "string",
                            "enum": ["connected_host", "managed_sandbox"],
                            "description": (
                                "Switch where firings run. 'managed_sandbox' provisions a "
                                "fresh server-managed sandbox per firing (clears any pinned "
                                "host; do not also set host_id). workspace may name a "
                                "repository URL, or be null for the provider default."
                            ),
                        },
                        "state": {
                            "type": "string",
                            "description": "'active' or 'paused'.",
                        },
                        "project_name": {
                            "type": "string",
                            "description": (
                                "File future fired sessions into this project, by name: "
                                "your existing project with this name, or a new one."
                            ),
                        },
                        "project_id": {
                            "type": ["string", "null"],
                            "description": (
                                "File future fired sessions into this existing project id. "
                                "Null stops filing them into a project."
                            ),
                        },
                    },
                    "required": ["scheduled_task_id", "prompt", "permission_mode"],
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
