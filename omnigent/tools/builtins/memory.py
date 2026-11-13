"""Built-in long-term memory tools.

Memory persists across sessions, harnesses, models and forks, and belongs to the
signed-in user. These tools are schema-only: the runner dispatches each to the
Omnigent server's ``/v1/memory`` routes (same posture as the scheduled-task and
policy tools), where the server decides whose memory it is. The model never
supplies an owner.

* ``memory_search`` — find relevant memories, on backends that can search.
* ``memory_get`` — read one memory by path.
* ``memory_list`` — list memory paths and summaries.
* ``memory_write`` — save or replace a memory.
* ``memory_delete`` — remove a memory.
"""

from __future__ import annotations

from typing import Any

from omnigent.stores.memory_store import MAX_DESCRIPTION_LENGTH, MAX_SEARCH_LIMIT
from omnigent.tools.base import Tool

_PATH_DESCRIPTION = (
    "Absolute path, e.g. '/user/preferences/testing' or '/projects/<repo>/gotchas'. "
    "Group related memories under a shared prefix."
)
_PREFIX_DESCRIPTION = (
    "Only paths at or under this prefix, matched on whole path segments, "
    "e.g. '/projects/api'. Omit for all memories."
)


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]):
    """Build an OpenAI-format function schema with no extra properties."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


class MemorySearchTool(Tool):
    """Search the user's memories. Runner-dispatched to ``POST /v1/memory/search``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"memory_search"``."""
        return "memory_search"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Search your long-term memory for notes saved in earlier sessions, on any "
            "harness or model: user preferences, project knowledge and procedures. "
            "Search before starting non-trivial work and when the user refers to "
            "something from the past."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "query": {"type": "string", "description": "Keywords to look for."},
                "path_prefix": {"type": "string", "description": _PREFIX_DESCRIPTION},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_LIMIT,
                    "description": "Maximum results (default 10).",
                },
            },
            ["query"],
        )


class MemoryGetTool(Tool):
    """Read one memory. Runner-dispatched to ``GET /v1/memory/entry``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"memory_get"``."""
        return "memory_get"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Read the full text of one memory by its path."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {"path": {"type": "string", "description": _PATH_DESCRIPTION}},
            ["path"],
        )


class MemoryListTool(Tool):
    """List memory paths. Runner-dispatched to ``GET /v1/memory/entries``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"memory_list"``."""
        return "memory_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List saved memories as paths with one-line summaries, without their full "
            "text. Use memory_get to read one. A result with a next_page_token has more: "
            "pass it back as page_token for the next page."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "path_prefix": {"type": "string", "description": _PREFIX_DESCRIPTION},
                "page_token": {
                    "type": "string",
                    "description": "next_page_token from the previous memory_list result.",
                },
            },
            [],
        )


class MemoryWriteTool(Tool):
    """Save a memory. Runner-dispatched to ``PUT /v1/memory/entry``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"memory_write"``."""
        return "memory_write"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Save something worth remembering in future sessions: a user preference, a "
            "durable fact about a project, or a procedure that worked. Writing to an "
            "existing path replaces it, so read it first to extend it. Do not save "
            "secrets, credentials or one-off task details."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "path": {"type": "string", "description": _PATH_DESCRIPTION},
                "content": {"type": "string", "description": "The memory, as markdown."},
                "description": {
                    "type": "string",
                    "maxLength": MAX_DESCRIPTION_LENGTH,
                    "description": "One-line summary returned by memory_list and memory_search.",
                },
            },
            ["path", "content"],
        )


class MemoryDeleteTool(Tool):
    """Delete a memory. Runner-dispatched to ``DELETE /v1/memory/entry``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"memory_delete"``."""
        return "memory_delete"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Delete a memory that is wrong or no longer useful."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {"path": {"type": "string", "description": _PATH_DESCRIPTION}},
            ["path"],
        )
