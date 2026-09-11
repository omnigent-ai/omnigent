"""Schema for the runner-owned ``sys_context_read`` tool."""

from __future__ import annotations

import json

from omnigent.tools.base import Tool, ToolContext


class SysContextReadTool(Tool):
    """Focused, read-only access to large workspace files."""

    @classmethod
    def name(cls) -> str:
        return "sys_context_read"

    @classmethod
    def description(cls) -> str:
        return (
            "Answer a specific question about large text files using Context Saver. "
            "Returns findings, relevant line ranges, short excerpts, and the worker model route."
        )

    def get_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Workspace-relative paths to inspect.",
                        },
                        "question": {
                            "type": "string",
                            "description": "The specific question to answer from the files.",
                        },
                        "technique": {
                            "type": "string",
                            "enum": ["focused_read"],
                            "description": "Optional Context Saver technique.",
                        },
                    },
                    "required": ["paths", "question"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return json.dumps({"error": "sys_context_read must be dispatched by the session runner"})
