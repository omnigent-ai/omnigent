"""Slack trigger dispatch: read the automation's state and start one worker per new match."""

from typing import Any

from omnigent.tools.base import Tool


class SysTriggerStateTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_trigger_state"

    @classmethod
    def description(cls) -> str:
        return (
            "Read this Slack trigger automation's state: each configured channel's ID (when "
            "known) and the time to read from, the saved workspace URL and author IDs, and the "
            "author and topic conditions you must apply yourself. Takes no arguments."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }


class SysTriggerDispatchTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_trigger_dispatch"

    @classmethod
    def description(cls) -> str:
        return (
            "Start one worker session for each new Slack message that matches this automation, "
            "oldest first, and save how far it has read. Pass the messages you read with "
            "conversations.history for each channel. The tool drops thread replies, bot and app "
            "messages, messages at or before the saved read time, and text that fails the text "
            "conditions; author and topic conditions are yours to apply before calling. Returns "
            "JSON with created, skipped, failed, read_from per channel and stopped_at_cap."
        )

    def get_schema(self) -> dict[str, Any]:
        optional_string = {"type": ["string", "null"]}
        message_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "ts": {
                    "type": "string",
                    "description": "Slack message ts, e.g. 1700000000.000100.",
                },
                "text": optional_string,
                "thread_ts": optional_string,
                "user": optional_string,
                "bot_id": optional_string,
                "subtype": optional_string,
            },
            "required": ["ts", "text"],
            "additionalProperties": False,
        }
        channel_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "The channel as sys_trigger_state named it.",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Slack channel ID, e.g. C0123ABCD.",
                },
                "messages": {"type": "array", "items": message_schema},
                "read_through_ts": {
                    "type": "string",
                    "description": "Newest ts you read, or the read_from you were given if none.",
                },
                "complete": {
                    "type": "boolean",
                    "description": "True only if you read every page from read_from on.",
                },
            },
            "required": ["channel", "channel_id", "messages", "read_through_ts", "complete"],
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channels": {"type": "array", "items": channel_schema, "minItems": 1},
                        "workspace_url": {
                            "type": "string",
                            "description": (
                                "The Slack workspace URL from auth.test or sys_trigger_state, "
                                "for building permalinks; saved so later runs skip auth.test."
                            ),
                        },
                        "author_ids": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": (
                                "Each author named in conditions_to_apply mapped to their Slack "
                                'user ID, e.g. {"Example Person": "U0123ABCD"}; saved so later '
                                "runs skip the lookups."
                            ),
                        },
                    },
                    "required": ["channels"],
                    "additionalProperties": False,
                },
            },
        }
