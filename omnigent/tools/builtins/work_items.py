"""Built-in tools for the Tasks / Work-Items feature.

Three LLM-callable tools backed by the :class:`WorkItemStore`:

- :class:`CreateWorkItemTool` (``create_work_item``) — create a tracked
  work item, idempotently by ``dedup_key`` (so the same external event
  ingested twice resolves to one row).
- :class:`ListWorkItemsTool` (``list_work_items``) — list work items, optionally
  filtered by status / conversation.
- :class:`UpdateWorkItemTool` (``update_work_item``) — update an item's
  status, PR URL, plan, title, or linked conversation.

These run AP-side (synchronous ``invoke``) and resolve the store lazily via
:func:`omnigent.runtime.get_work_item_store`, mirroring the other
store-backed builtins (e.g. ``search_conversations``).
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.entities import WORK_ITEM_SOURCES, WORK_ITEM_STATUSES, WorkItem
from omnigent.tools.base import Tool, ToolContext

# Sorted for stable schema enums / deterministic error messages.
_SOURCES = sorted(WORK_ITEM_SOURCES)
_STATUSES = sorted(WORK_ITEM_STATUSES)


def _item_dict(item: WorkItem) -> dict[str, Any]:
    """
    Serialize a :class:`WorkItem` to a JSON-friendly dict for tool output.

    :param item: The work item to serialize.
    :returns: A dict of the item's fields.
    """
    return {
        "id": item.id,
        "source": item.source,
        "external_id": item.external_id,
        "dedup_key": item.dedup_key,
        "title": item.title,
        "status": item.status,
        "pr_url": item.pr_url,
        "conversation_id": item.conversation_id,
        "assignee_user_id": item.assignee_user_id,
        "plan": item.plan,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _store_or_error() -> tuple[Any, str | None]:
    """
    Resolve the work-item store, or an error message if unconfigured.

    :returns: ``(store, None)`` when available, else ``(None, json_error)``.
    """
    from omnigent.runtime import get_work_item_store

    store = get_work_item_store()
    if store is None:
        return None, json.dumps({"error": "work item store is not configured on this server"})
    return store, None


class CreateWorkItemTool(Tool):
    """Create a tracked work item (idempotent by ``dedup_key``)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"create_work_item"``."""
        return "create_work_item"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return (
            "Create a tracked work item (a task) — e.g. turning a Slack "
            "message, email, or GitHub/Jira issue into something an agent "
            "can work on. Pass a stable dedup_key (e.g. 'github:org/repo#123') "
            "to make repeated ingestion idempotent: a second call with the "
            "same dedup_key returns the existing item instead of creating a "
            "duplicate."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``create_work_item``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short task title."},
                        "source": {
                            "type": "string",
                            "enum": _SOURCES,
                            "description": "Where the task came from.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Optional longer description / original text.",
                        },
                        "dedup_key": {
                            "type": "string",
                            "description": (
                                "Stable idempotency key, e.g. 'github:org/repo#123'. "
                                "Omit for a one-off manual task (a unique key is generated)."
                            ),
                        },
                        "external_id": {
                            "type": "string",
                            "description": "Source-native id (issue/PR number, message ts).",
                        },
                        "status": {
                            "type": "string",
                            "enum": _STATUSES,
                            "description": "Initial status (default 'new').",
                        },
                        "conversation_id": {
                            "type": "string",
                            "description": "Conversation/sub-session that will process this item.",
                        },
                        "plan": {
                            "type": "string",
                            "description": "Optional plan for the work.",
                        },
                    },
                    "required": ["title", "source"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Create (or resolve an existing) work item; return it as JSON."""
        del ctx
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        title = args.get("title")
        source = args.get("source")
        if not isinstance(title, str) or not title.strip():
            return json.dumps({"error": "title is required"})
        if source not in WORK_ITEM_SOURCES:
            return json.dumps({"error": f"source must be one of {_SOURCES}"})

        status = args.get("status", "new")
        if status not in WORK_ITEM_STATUSES:
            return json.dumps({"error": f"status must be one of {_STATUSES}"})

        external_id = args.get("external_id")
        from omnigent.db.utils import generate_work_item_id

        work_item_id = generate_work_item_id()
        # Idempotency key: caller-supplied, else derived from source+external_id,
        # else the freshly-minted id (so a keyless manual task always inserts).
        dedup_key = args.get("dedup_key")
        if not dedup_key:
            dedup_key = f"{source}:{external_id}" if external_id else f"manual:{work_item_id}"

        store, err = _store_or_error()
        if err is not None:
            return err

        existing = store.get_by_dedup_key(dedup_key)
        if existing is not None:
            return json.dumps({"created": False, "work_item": _item_dict(existing)})

        item = store.create(
            work_item_id,
            source,
            title.strip(),
            dedup_key=dedup_key,
            external_id=external_id,
            body=args.get("body"),
            status=status,
            conversation_id=args.get("conversation_id"),
            plan=args.get("plan"),
        )
        return json.dumps({"created": True, "work_item": _item_dict(item)})


class ListWorkItemsTool(Tool):
    """List work items, optionally filtered by status / conversation."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"list_work_items"``."""
        return "list_work_items"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return (
            "List tracked work items (tasks), newest first. Optionally filter "
            "by status or by the conversation processing them."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``list_work_items``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": _STATUSES,
                            "description": "Only items in this status.",
                        },
                        "conversation_id": {
                            "type": "string",
                            "description": "Only items linked to this conversation.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max items to return (default 50).",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """List work items as JSON."""
        del ctx
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        status = args.get("status")
        if status is not None and status not in WORK_ITEM_STATUSES:
            return json.dumps({"error": f"status must be one of {_STATUSES}"})

        limit_raw = args.get("limit", 50)
        try:
            limit = max(1, min(int(limit_raw), 1000))
        except (TypeError, ValueError):
            return json.dumps({"error": "limit must be an integer"})

        store, err = _store_or_error()
        if err is not None:
            return err

        items = store.list(
            status=status,
            conversation_id=args.get("conversation_id"),
            limit=limit,
        )
        return json.dumps({"count": len(items), "work_items": [_item_dict(i) for i in items]})


class UpdateWorkItemTool(Tool):
    """Update a work item's status / PR URL / plan / title / conversation."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"update_work_item"``."""
        return "update_work_item"

    @classmethod
    def description(cls) -> str:
        """:returns: Description visible to the LLM."""
        return (
            "Update a work item: change its status, attach a PR URL, record a "
            "plan, rename it, or link the conversation working on it. Pass "
            "needs_review=true as a shortcut to set status='needs_review'."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI tool schema for ``update_work_item``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "work_item_id": {
                            "type": "string",
                            "description": "The id of the work item to update.",
                        },
                        "status": {
                            "type": "string",
                            "enum": _STATUSES,
                            "description": "New status.",
                        },
                        "needs_review": {
                            "type": "boolean",
                            "description": "Shortcut for status='needs_review'.",
                        },
                        "pr_url": {"type": "string", "description": "Pull-request URL."},
                        "plan": {"type": "string", "description": "Updated plan text."},
                        "title": {"type": "string", "description": "New title."},
                        "conversation_id": {
                            "type": "string",
                            "description": "Conversation/sub-session processing this item.",
                        },
                    },
                    "required": ["work_item_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Update a work item; return the updated item as JSON."""
        del ctx
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        work_item_id = args.get("work_item_id")
        if not isinstance(work_item_id, str) or not work_item_id:
            return json.dumps({"error": "work_item_id is required"})

        status = args.get("status")
        if args.get("needs_review") is True and status is None:
            status = "needs_review"
        if status is not None and status not in WORK_ITEM_STATUSES:
            return json.dumps({"error": f"status must be one of {_STATUSES}"})

        store, err = _store_or_error()
        if err is not None:
            return err

        updated = store.update(
            work_item_id,
            status=status,
            pr_url=args.get("pr_url"),
            plan=args.get("plan"),
            title=args.get("title"),
            conversation_id=args.get("conversation_id"),
        )
        if updated is None:
            return json.dumps({"error": "not_found", "work_item_id": work_item_id})
        return json.dumps({"work_item": _item_dict(updated)})
