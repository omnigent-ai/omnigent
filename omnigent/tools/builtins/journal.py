"""Tools an agent uses to read and write its session's journal.

* ``sys_journal_read`` reads a journal: the plan, step statuses and notes.
* ``sys_journal_write_plan`` sets the session's plan, the whole graph each time.
* ``sys_journal_write_status`` records where one plan step stands.
* ``sys_journal_write_note`` adds a Markdown note.

All four are schema-only: the runner sends them to the server's
``/v1/sessions/{id}/journal`` routes. They make up the ``journal`` tool group,
which every agent has unless its spec sets ``tool_groups.journal: false``.
"""

from __future__ import annotations

from typing import Any

from omnigent.entities.session_journal import (
    JOURNAL_KINDS,
    NOTE_BODY_MAX_BYTES,
    PLAN_MAX_STEPS,
    PLAN_NAME_MAX_CHARS,
    STATUS_TEXT_MAX_CHARS,
    STEP_GOAL_MAX_CHARS,
    STEP_ID_MAX_CHARS,
    STEP_NAME_MAX_CHARS,
    STEP_STATUSES,
)
from omnigent.tools.base import Tool

# The most entries one sys_journal_read returns.
JOURNAL_READ_PAGE_MAX = 100

_STEP_ID_PATTERN = "^[a-z0-9][a-z0-9_-]{0,63}$"
_STEP_DESC = (
    "A plan step's ID, e.g. 'propose': lowercase letters, digits, '-' and '_'. "
    "Keep IDs stable across replans."
)


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
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


class SysJournalReadTool(Tool):
    """Read a session's journal. Runner-dispatched to ``GET /v1/sessions/{id}/journal``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_journal_read"``."""
        return "sys_journal_read"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Read a session's journal: its plan versions, step statuses and notes, as compact "
            "lines, oldest first within the page. With no arguments it returns your own "
            "journal's newest entries; page.next_cursor pages back to older ones. Read it "
            "first when you resume work, so you don't redo finished steps. kind='plan' "
            "returns plan entries with their full graphs; step limits the read to one "
            "step. conversation_id reads another session you can read, such as a worker."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "conversation_id": {
                    "type": "string",
                    "description": "The session to read. Omit it to read your own journal.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(JOURNAL_KINDS),
                    "description": "Only entries of this kind.",
                },
                "step": {
                    "type": "string",
                    "maxLength": STEP_ID_MAX_CHARS,
                    "description": "Only entries about this plan step.",
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": (
                        "page.next_cursor from a prior sys_journal_read, to read older entries."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": JOURNAL_READ_PAGE_MAX,
                    "description": "The most entries to return.",
                },
            },
            [],
        )


class SysJournalWritePlanTool(Tool):
    """Set the session's plan. Runner-dispatched to ``POST /v1/sessions/{id}/journal``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_journal_write_plan"``."""
        return "sys_journal_write_plan"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Set your session's plan: its name and the whole graph of steps and their "
            "dependencies, on every call. It becomes the current plan; earlier versions stay "
            "in the journal, and the user watches its steps in the session's Tasks tab, under "
            "its name. A single goal is a one-step plan. Replan when the goal grows or "
            "changes; keep the name while the goal is the same. A step that keeps its ID "
            "keeps its status, so finished work stays finished; give a step a new ID only "
            "when its work changes. Returns the plan's version."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": PLAN_NAME_MAX_CHARS,
                    "description": (
                        "The plan's goal in a few words, e.g. 'Cut GET /orders p95 by 20%'."
                    ),
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": PLAN_MAX_STEPS,
                    "description": (
                        "Every step of the plan, with unique IDs and no dependency cycles."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "pattern": _STEP_ID_PATTERN,
                                "description": _STEP_DESC,
                            },
                            "name": {
                                "type": "string",
                                "maxLength": STEP_NAME_MAX_CHARS,
                                "description": "A short name, e.g. 'Profile the endpoint'.",
                            },
                            "goal": {
                                "type": "string",
                                "maxLength": STEP_GOAL_MAX_CHARS,
                                "description": "What done means for this step.",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string", "pattern": _STEP_ID_PATTERN},
                                "description": (
                                    "IDs of steps that must be done before this one starts."
                                ),
                            },
                        },
                        "required": ["id", "name", "depends_on"],
                        "additionalProperties": False,
                    },
                },
            },
            ["name", "steps"],
        )


class SysJournalWriteStatusTool(Tool):
    """Record a step's status. Runner-dispatched to ``POST /v1/sessions/{id}/journal``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_journal_write_status"``."""
        return "sys_journal_write_status"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Record where a plan step stands; the newest status is the step's current one. "
            "'started' when you begin a step yourself, or resume it; 'done' when it achieved "
            "its goal (only done lets later steps start); 'needs_input' when it waits for "
            "the user (an approval, a decision, or a go-ahead they asked for): write it, "
            "then ask them with your question tool so it reaches their Inbox; 'blocked' when "
            "something outside the session stops it, such as CI being down; 'failed' when "
            "its own work failed. text says why in a few words, and is required except for "
            "started. Writing started on a done step reopens it. started and done need "
            "every step this one depends on to be done first; otherwise the write fails "
            "with step_not_ready, naming them. A user stopping you is neither failed nor "
            "blocked: leave the step started."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "status": {"type": "string", "enum": list(STEP_STATUSES)},
                "step": {
                    "type": "string",
                    "pattern": _STEP_ID_PATTERN,
                    "description": _STEP_DESC + " May be left out when the plan has one step.",
                },
                "text": {
                    "type": "string",
                    "maxLength": STATUS_TEXT_MAX_CHARS,
                    "description": (
                        "Why, e.g. 'p95 430 ms', 'CI is down' or 'Approve the deploy to prod?'."
                    ),
                },
            },
            ["status"],
        )


class SysJournalWriteNoteTool(Tool):
    """Add a note to the journal. Runner-dispatched to ``POST /v1/sessions/{id}/journal``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_journal_write_note"``."""
        return "sys_journal_write_note"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Add a note to your session's journal: Markdown whose first line is the summary. "
            "Use notes for decisions, measurements and what to check when resuming, and "
            "write one before an external side effect such as a push, merge or deploy. "
            "Point to files, PRs and sessions instead of pasting large content. step "
            "attaches the note to a plan step."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "body": {
                    "type": "string",
                    "maxLength": NOTE_BODY_MAX_BYTES,
                    "description": (
                        f"Markdown, first line first; at most {NOTE_BODY_MAX_BYTES} bytes."
                    ),
                },
                "step": {"type": "string", "pattern": _STEP_ID_PATTERN, "description": _STEP_DESC},
            },
            ["body"],
        )


JOURNAL_TOOLS: tuple[type[Tool], ...] = (
    SysJournalReadTool,
    SysJournalWritePlanTool,
    SysJournalWriteStatusTool,
    SysJournalWriteNoteTool,
)
