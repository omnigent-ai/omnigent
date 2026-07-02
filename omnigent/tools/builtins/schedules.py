"""Built-in tools for scheduled work (loops).

Three LLM-callable tools backed by the :class:`ScheduleStore`:

- :class:`CreateLoopTool` (``create_loop``) — a cron-driven recurring prompt
  (e.g. a Friday-night weekly report).
- :class:`ListSchedulesTool` (``list_schedules``) — list a conversation's loops.
- :class:`DeleteScheduleTool` (``delete_schedule``) — remove one by id.

These run AP-side (synchronous ``invoke``) and resolve the store lazily via
:func:`omnigent.runtime.get_schedule_store`. Creating a loop persists the
definition and arms it on the next scheduler pass; firing is handled by the
scheduler service (not these tools).

Monitors (streaming a command and firing a prompt per output line) are a
planned follow-up: they need host-side subprocess supervision, so the
``create_monitor`` tool is intentionally not shipped here. The ``schedules``
table keeps its ``kind``/``command`` columns as the foundation for that work.
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
        "agent_name": s.agent_name,
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
            "Schedule a recurring PROMPT on a cron cadence (5-field cron, e.g. "
            "'*/10 * * * *' = every 10 min, '0 9 * * 1-5' = 9am weekdays, "
            "'0 22 * * FRI' = Fridays at 22:00). Use for periodic work, checks, "
            "or reports — each time it fires, the prompt runs as a fresh turn in "
            "the conversation and the agent sees the full output. Prefer this "
            "over ad-hoc shell loops (e.g. 'while true; do …; sleep'), which "
            "block the session and leave no tracked run. By default it fires in "
            "this conversation; pass 'agent' (a registered agent name) to make "
            "it a GLOBAL loop that spawns a FRESH run with that agent each fire "
            "instead. Names are unique per conversation."
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
                            "description": "Target conversation (defaults to the current one). "
                            "Omit and set 'agent' for a global loop.",
                        },
                        "agent": {
                            "type": "string",
                            "description": "Registered agent name. When set, the loop is GLOBAL: "
                            "each fire spawns a fresh run with this agent, not tied to a "
                            "conversation.",
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

        store, err = _store_or_error()
        if err is not None:
            return err

        # This in-process path has no acting-user identity (ToolContext carries
        # none), so it can't OWN a spawned run — and a global loop's run must be
        # owned to be visible. Global loops are therefore created via the
        # authenticated API/relay path (the runner proxies create_loop with an
        # 'agent' to POST /v1/schedules, which captures the user). Here we
        # support conversation-scoped loops only.
        agent = args.get("agent")
        if isinstance(agent, str) and agent.strip():
            return json.dumps(
                {
                    "error": "global loops (with 'agent') must be created via the API; "
                    "this path supports conversation-scoped loops only"
                }
            )
        conversation_id = _resolve_conversation_id(args, ctx)
        if not conversation_id:
            return json.dumps({"error": "create_loop requires a conversation context"})

        # Validate the cron up front so a typo doesn't silently disarm at fire time.
        from datetime import datetime, timezone

        from omnigent.runtime.cron import next_cron_time

        try:
            next_cron_time(cron, datetime.now(timezone.utc))
        except ValueError as exc:
            return json.dumps({"error": f"invalid cron expression: {exc}"})

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
