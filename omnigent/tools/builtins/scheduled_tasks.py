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
* ``sys_reminder_set`` — send a message into the calling session once, at a
  given time. It is a scheduled task with a one-time ``COUNT=1`` rule that
  continues the session, so it survives runner exits, unlike ``sys_timer_set``.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from omnigent.tools.base import Tool

# A reminder must fire after the task exists; rounding up to the minute adds up to
# another minute.
MIN_REMINDER_DELAY = timedelta(seconds=60)
# Below one year, so the rule's month, day and time occur only once after creation.
MAX_REMINDER_DELAY = timedelta(days=364)
REMINDER_NAME_CHARS = 60

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
                                "'ask-for-approval', 'approve-for-me', 'read-only'), "
                                "Cursor (e.g. 'auto-review'), "
                                "Antigravity ('default', 'skip'), or Devin (e.g. 'smart'). "
                                "Other harnesses, such as claude-sdk, have no automation "
                                "permission modes. "
                                "Runs are unattended, so a prompting mode ('default'/'plan') "
                                "stalls waiting for approval — prefer an auto-running mode. "
                                "Send JSON null (not the string 'null') for the agent "
                                "default, and always for a harness without modes. This "
                                "field is required so the user can review the effective "
                                "task setting."
                            ),
                        },
                        "mcp_services": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional MCP services every run starts with, as the "
                                "deployment's service names. The owner must already be "
                                "connected to each one. Omit for none."
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
                                "or Devin (e.g. 'smart'). Other harnesses, such as "
                                "claude-sdk, have no automation permission modes. Send JSON "
                                "null (not the string 'null') for the agent default, and "
                                "always for a harness without modes. Required for approval "
                                "review; resend the current value when it is unchanged."
                            ),
                        },
                        "mcp_services": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Replaces the MCP services every run starts with. "
                                "An empty list clears them."
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


def resolve_reminder_time(args: dict[str, Any], now: datetime) -> datetime | str:
    """Resolve a ``sys_reminder_set`` call's fire time, rounded up to the minute.

    :param args: Parsed tool arguments with exactly one of ``at`` (ISO-8601 with
        an offset, e.g. ``"2026-11-12T15:00:00-08:00"``) or ``delay_seconds``.
    :param now: The current time, timezone-aware.
    :returns: The fire time in UTC on a whole minute, or an error message.
    """
    has_at = args.get("at") is not None
    has_delay = args.get("delay_seconds") is not None
    if has_at == has_delay:
        return "Set exactly one of 'at' or 'delay_seconds'."
    if has_at:
        raw = args["at"]
        if not isinstance(raw, str):
            return "'at' must be an ISO-8601 string, e.g. '2026-11-12T15:00:00-08:00'."
        try:
            when = datetime.fromisoformat(raw.strip())
        except ValueError:
            return f"'at' is not an ISO-8601 time: {raw!r}."
        if when.tzinfo is None or when.utcoffset() is None:
            return "'at' needs a UTC offset or 'Z', e.g. '2026-11-12T15:00:00-08:00'."
        fraction = re.search(r"[.,](\d+)", raw)
        # fromisoformat drops digits past microseconds; count them so the time
        # can't round down to a minute before the one asked for.
        sub_microsecond = fraction is not None and bool(fraction.group(1)[6:].strip("0"))
    else:
        delay = args["delay_seconds"]
        # bool is an int subclass; reject it like sys_timer_set does.
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            return "'delay_seconds' must be a number."
        # Bound the value before any conversion: a huge int overflows float(), and
        # timedelta() overflows well inside the finite float range.
        if isinstance(delay, float) and not math.isfinite(delay):
            return "'delay_seconds' must be a finite number."
        if delay > MAX_REMINDER_DELAY.total_seconds():
            return f"A reminder can be at most {MAX_REMINDER_DELAY.days} days away."
        if delay < 0:
            return "A reminder must be at least one minute in the future."
        when = now + timedelta(seconds=delay)
        sub_microsecond = False
    try:
        if sub_microsecond:
            when += timedelta(microseconds=1)
        when = when.astimezone(UTC)
        # The upper limit applies before rounding, so the largest delay is accepted;
        # the extra seconds still leave the time well under a year away.
        if when > now + MAX_REMINDER_DELAY:
            return f"A reminder can be at most {MAX_REMINDER_DELAY.days} days away."
        if when.second or when.microsecond:
            when = when.replace(second=0, microsecond=0) + timedelta(minutes=1)
    except OverflowError:
        # Only an 'at' near the ends of the datetime range gets here.
        return "'at' is outside the supported date range."
    if when < now + MIN_REMINDER_DELAY:
        return "A reminder must be at least one minute in the future."
    return when


def reminder_rrule(when: datetime) -> str:
    """The one-time rule that fires at *when* (UTC, whole minute).

    The server counts ``COUNT`` from the task's creation, and *when* is less than a
    year away, so the first match of this month, day and time is *when* itself.

    :param when: The fire time, e.g. ``datetime(2026, 11, 12, 23, 0, tzinfo=UTC)``.
    :returns: The rule, e.g. for 23:00 UTC on November 12:
        ``"FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=12;BYHOUR=23;BYMINUTE=0;BYSECOND=0;COUNT=1"``.
    """
    when = when.astimezone(UTC)
    return (
        f"FREQ=YEARLY;BYMONTH={when.month};BYMONTHDAY={when.day};"
        f"BYHOUR={when.hour};BYMINUTE={when.minute};BYSECOND=0;COUNT=1"
    )


def reminder_prompt(message: str, when: datetime) -> str:
    """The text the reminder sends into the session when it fires."""
    return f"[Reminder set for {when.astimezone(UTC):%Y-%m-%d %H:%M} UTC]\n{message}"


def reminder_name(message: str) -> str:
    """The automation name a reminder is listed under."""
    first_line = " ".join(message.split())
    if len(first_line) > REMINDER_NAME_CHARS:
        first_line = first_line[: REMINDER_NAME_CHARS - 3] + "..."
    return f"Reminder: {first_line}"


class SysReminderSetTool(Tool):
    """Remind the calling session once. Runner-dispatched to ``POST /v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_reminder_set"``."""
        return "sys_reminder_set"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Set a one-time reminder: at the given time, the message is sent into "
            "this session as a new turn, even if the session has gone idle, the "
            "runner has stopped, or the server restarted in between. The session's "
            "host must be online then; a reminder that can't be delivered, "
            "including one whose session was deleted, is recorded as a failed run "
            "and not retried. Give either "
            "'at' (an ISO-8601 time with a UTC offset) or 'delay_seconds'. The "
            "requested time may be at most 364 days away, and is rounded up to the "
            "next minute, which can add less than a minute; the result must be at "
            "least a minute away. Write the message for your future self: "
            "say what to check or do. The Automations page doesn't list "
            "reminders; sys_scheduled_task_list does. Cancel one with "
            "sys_scheduled_task_delete and the returned "
            "scheduled_task_id. Works in top-level sessions you own on a "
            "connected host, not in sub-agent sessions, managed sandboxes, or "
            "sessions running in a permission mode that skips approval. For "
            "a short wait inside an active turn, sys_timer_set (when enabled) is "
            "lighter."
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
                        "message": {
                            "type": "string",
                            "description": (
                                "What the reminder says when it fires, e.g. "
                                "'Check whether CI on PR #123 passed and report back.'"
                            ),
                        },
                        "at": {
                            "type": "string",
                            "description": (
                                "When to fire, as ISO-8601 with a UTC offset or 'Z', "
                                "e.g. '2026-11-12T15:00:00-08:00'. Do not set "
                                "delay_seconds with it."
                            ),
                        },
                        "delay_seconds": {
                            "type": "number",
                            "description": (
                                "When to fire, as seconds from when the call runs (after "
                                "its approval), e.g. 2700 for 45 minutes. Do not set at "
                                "with it."
                            ),
                        },
                    },
                    "required": ["message"],
                    "additionalProperties": False,
                },
            },
        }
