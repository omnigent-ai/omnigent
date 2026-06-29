"""Built-in tools for scheduled work (loops & monitors).

Four LLM-callable tools backed by the :class:`ScheduleStore`:

- :class:`CreateLoopTool` (``create_loop``) — a cron-driven recurring prompt
  (e.g. a Friday-night weekly report).
- :class:`CreateMonitorTool` (``create_monitor``) — stream a shell command and
  fire a prompt per output line.
- :class:`ListSchedulesTool` (``list_schedules``) — list a conversation's
  loops & monitors.
- :class:`DeleteScheduleTool` (``delete_schedule``) — remove one by id.

These run AP-side (synchronous ``invoke``) and resolve the store lazily via
:func:`omnigent.runtime.get_schedule_store`. Creating a schedule persists the
definition and arms it on the next scheduler pass; firing is handled by the
scheduler service (not these tools).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.exc import IntegrityError

from omnigent.entities.schedule import Schedule
from omnigent.tools.base import Tool, ToolContext


def _schedule_dict(s: Schedule) -> dict[str, Any]:
    """
    Serialize a :class:`Schedule` to a JSON-friendly dict.

    :param s: The schedule to serialize.
    :returns: A dict of the schedule's fields.
    """
    return {
        "id": s.id,
        "conversation_id": s.conversation_id,
        "name": s.name,
        "kind": s.kind,
        "prompt": s.prompt,
        "cron": s.cron,
        "command": s.command,
        "enabled": s.enabled,
        "status": s.status,
        "last_fired_at": s.last_fired_at,
        "last_run_id": s.last_run_id,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _store_or_error() -> tuple[Any, str | None]:
    """
    Resolve the schedule store, or an error message if unconfigured.

    :returns: ``(store, None)`` when available, else ``(None, json_error)``.
    """
    from omnigent.runtime import get_schedule_store

    store = get_schedule_store()
    if store is None:
        return None, json.dumps({"error": "schedule store is not configured on this server"})
    return store, None


def _resolve_conversation_id(args: dict[str, Any], ctx: ToolContext) -> str | None:
    """
    Pick the target conversation: explicit arg, else the current context.

    :param args: Parsed tool arguments.
    :param ctx: Tool execution context.
    :returns: A conversation id, or ``None`` if neither is available.
    """
    explicit = args.get("conversation_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    return ctx.conversation_id


class CreateLoopTool(Tool):
    """Create a cron-driven recurring prompt (a loop)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"create_loop"``."""
        return "create_loop"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return (
            "Schedule a recurring prompt on a cron cadence — e.g. a weekly "
            "report every Friday at 22:00 ('0 22 * * FRI'). The loop fires in "
            "this conversation unless conversation_id is given. Names are unique "
            "per conversation."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``create_loop``."""
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
                            "description": "Loop name (unique per conversation).",
                        },
                        "prompt": {"type": "string", "description": "Prompt fired on each tick."},
                        "cron": {
                            "type": "string",
                            "description": "Cron expression, e.g. '0 22 * * FRI'.",
                        },
                        "conversation_id": {
                            "type": "string",
                            "description": "Target conversation (defaults to the current one).",
                        },
                    },
                    "required": ["name", "prompt", "cron"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Create a loop schedule; return it as JSON."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        name = args.get("name")
        prompt = args.get("prompt")
        cron = args.get("cron")
        if not all(isinstance(v, str) and v.strip() for v in (name, prompt, cron)):
            return json.dumps({"error": "name, prompt, and cron are required"})

        conversation_id = _resolve_conversation_id(args, ctx)
        if not conversation_id:
            return json.dumps({"error": "create_loop requires a conversation context"})

        store, err = _store_or_error()
        if err is not None:
            return err

        from omnigent.db.utils import generate_schedule_id

        try:
            sched = store.create(
                generate_schedule_id(),
                conversation_id,
                name.strip(),
                "loop",
                prompt,
                cron=cron,
            )
        except IntegrityError:
            return json.dumps(
                {"error": f"a schedule named {name!r} already exists in this conversation"}
            )
        return json.dumps({"schedule": _schedule_dict(sched)})


class CreateMonitorTool(Tool):
    """Create a monitor: stream a command and fire a prompt per line."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"create_monitor"``."""
        return "create_monitor"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return (
            "Stream a shell command and fire a prompt for each new output line "
            "(e.g. tail a log and react to errors). The prompt may reference the "
            "triggering line as {line}. Runs in this conversation unless "
            "conversation_id is given. Names are unique per conversation."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``create_monitor``."""
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
                            "description": "Monitor name (unique per conversation).",
                        },
                        "command": {
                            "type": "string",
                            "description": "Shell command to stream, e.g. 'tail -f app.log'.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Prompt template fired per line (may use {line}).",
                        },
                        "conversation_id": {
                            "type": "string",
                            "description": "Target conversation (defaults to the current one).",
                        },
                    },
                    "required": ["name", "command", "prompt"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Create a monitor schedule; return it as JSON."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        name = args.get("name")
        prompt = args.get("prompt")
        command = args.get("command")
        if not all(isinstance(v, str) and v.strip() for v in (name, prompt, command)):
            return json.dumps({"error": "name, command, and prompt are required"})

        conversation_id = _resolve_conversation_id(args, ctx)
        if not conversation_id:
            return json.dumps({"error": "create_monitor requires a conversation context"})

        store, err = _store_or_error()
        if err is not None:
            return err

        from omnigent.db.utils import generate_schedule_id

        try:
            sched = store.create(
                generate_schedule_id(),
                conversation_id,
                name.strip(),
                "monitor",
                prompt,
                command=command,
            )
        except IntegrityError:
            return json.dumps(
                {"error": f"a schedule named {name!r} already exists in this conversation"}
            )
        return json.dumps({"schedule": _schedule_dict(sched)})


class ListSchedulesTool(Tool):
    """List a conversation's loops & monitors."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"list_schedules"``."""
        return "list_schedules"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return (
            "List the loops and monitors for this conversation (or another, via conversation_id)."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``list_schedules``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {
                            "type": "string",
                            "description": "Conversation to list (defaults to the current one).",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """List schedules as JSON."""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        conversation_id = _resolve_conversation_id(args, ctx)
        if not conversation_id:
            return json.dumps({"error": "list_schedules requires a conversation context"})

        store, err = _store_or_error()
        if err is not None:
            return err

        items = store.list_for_conversation(conversation_id)
        return json.dumps({"count": len(items), "schedules": [_schedule_dict(s) for s in items]})


class DeleteScheduleTool(Tool):
    """Delete a loop or monitor by id."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"delete_schedule"``."""
        return "delete_schedule"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return "Delete a loop or monitor by its schedule id. Idempotent."

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``delete_schedule``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_id": {
                            "type": "string",
                            "description": "The id of the schedule to delete.",
                        },
                    },
                    "required": ["schedule_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Delete a schedule; return whether a row was removed."""
        del ctx
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        schedule_id = args.get("schedule_id")
        if not isinstance(schedule_id, str) or not schedule_id:
            return json.dumps({"error": "schedule_id is required"})

        store, err = _store_or_error()
        if err is not None:
            return err

        deleted = store.delete(schedule_id)
        return json.dumps({"deleted": deleted, "schedule_id": schedule_id})
