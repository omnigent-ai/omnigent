"""Built-in tools that write to and read the session owner's Inbox.

Registered only when the agent spec sets ``tool_groups.user_inbox: true``.
The runner forwards both to the server's ``/v1/inbox/items`` endpoints.
Not to be confused with ``sys_read_inbox``, which drains an agent's own
queue of background-work results.

``sys_user_inbox_post``
    Post one suggestion to the owner's Inbox.

``sys_user_inbox_list``
    List the agent's recent suggestions with the owner's responses.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# How far back sys_user_inbox_list reads, so the agent sees what it already posted.
USER_INBOX_LIST_WINDOW_DAYS = 30


class SysUserInboxPostTool(Tool):
    """Post a suggestion to the owner's Inbox. Dispatched to ``POST /v1/inbox/items``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_user_inbox_post"``."""
        return "sys_user_inbox_post"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Post one suggestion to the user's Inbox: a follow-up, a piece of information, "
            "or a reminder. Call sys_user_inbox_list first and skip anything the user already "
            "has. Reuse an existing item's topic when posting about the same thing. If the "
            "server reports duplicate: true, the item already exists; do not post it again."
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
                        "kind": {
                            "type": "string",
                            "enum": ["followup", "info", "reminder"],
                            "description": "What the item is.",
                        },
                        "title": {
                            "type": "string",
                            "description": "One-line summary, under 200 characters.",
                        },
                        "body_md": {
                            "type": "string",
                            "description": "Markdown body: what and why, under 8000 characters.",
                        },
                        "links": {
                            "type": "array",
                            "description": (
                                "Evidence. Put the thing the item is about first, e.g. the "
                                "pull request or session."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["session", "pr", "url"]},
                                    "ref": {
                                        "type": "string",
                                        "description": "Session id or URL.",
                                    },
                                    "label": {"type": "string"},
                                },
                                "required": ["type", "ref"],
                                "additionalProperties": False,
                            },
                        },
                        "topic": {
                            "type": "string",
                            "description": (
                                "Short slug naming what the item is about, e.g. "
                                "'rampup-flag'. Together with kind and the first link it "
                                "identifies the item, so reuse it for the same subject."
                            ),
                        },
                        "actions": {
                            "type": "array",
                            "description": (
                                "Up to three buttons. Each opens a new session prefilled "
                                "with its prompt for the user to edit and send, e.g. a "
                                "reply to draft or a change to make."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Button text, under 60 characters.",
                                    },
                                    "prompt": {
                                        "type": "string",
                                        "description": "Prompt the new session starts with.",
                                    },
                                },
                                "required": ["label", "prompt"],
                                "additionalProperties": False,
                            },
                        },
                        "chart": {
                            "type": "object",
                            "description": (
                                "Optional chart drawn on the card, only when it shows the "
                                "point of the item. Use only numbers from queries you ran, "
                                "and state the key numbers in body_md too. Shape: {version: "
                                "1, type: 'line' | 'bar', title, unit?, x: [labels, at most "
                                "60], series: [{name, values: [number or null, one per x "
                                "label], emphasis?}] (at most 4), highlight?: {from, to} "
                                "(x labels), markers?: [{x, label}] (at most 3), source?: "
                                "{label, href?}}."
                            ),
                        },
                        "expires_at": {
                            "type": "integer",
                            "description": (
                                "Optional Unix time after which the same item may be posted "
                                "again, for reminders tied to a date."
                            ),
                        },
                    },
                    "required": ["kind", "title", "body_md", "topic"],
                    "additionalProperties": False,
                },
            },
        }


class SysUserInboxListTool(Tool):
    """List recent Inbox items. Dispatched to ``GET /v1/inbox/items``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_user_inbox_list"``."""
        return "sys_user_inbox_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            f"List the user's Inbox items from the last {USER_INBOX_LIST_WINDOW_DAYS} days in "
            "every state, with each item's topic and how the user responded: read, an action "
            "taken, or a Dismiss reason (already done, useful, not relevant, incorrect, not "
            "now). Use it before posting to avoid repeats and to learn what the user wants."
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
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }
