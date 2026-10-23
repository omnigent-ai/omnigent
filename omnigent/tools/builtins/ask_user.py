"""Structured, non-blocking questions delivered through the conversation."""

from __future__ import annotations

from typing import Any

from omnigent.entities.conversation import AsyncUserInput
from omnigent.tools.base import Tool


class SysAskUserTool(Tool):
    """Schema-only tool; the runner persists questions and returns an ACK."""

    @classmethod
    def name(cls) -> str:
        """Return the framework-owned tool name."""
        return "sys_ask_user"

    @classmethod
    def description(cls) -> str:
        """Describe asynchronous answers without implying authorization."""
        return (
            "Ask the user structured questions in the current conversation. "
            "Non-blocking: returns an acknowledgement immediately; the user's "
            "selected or custom answers arrive later as an ordinary user message. "
            "Continue independent work, but wait for the answers before doing work "
            "that depends on them. This tool does not request or grant authorization."
        )

    def get_schema(self) -> dict[str, Any]:
        """Reuse the persisted question schema without exposing runtime identity."""
        parameters = AsyncUserInput.model_json_schema()
        parameters["properties"] = {"questions": parameters["properties"]["questions"]}
        parameters["required"] = ["questions"]
        parameters.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": parameters,
            },
        }
