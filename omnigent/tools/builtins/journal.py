"""Tools an agent uses to read and write its session's journal.

* ``sys_journal_read`` reads a journal: the plan, step statuses and notes.
* ``sys_journal_write_plan`` sets the session's plan, the whole graph each time.
* ``sys_journal_write_status`` records where one plan step stands.
* ``sys_journal_write_note`` adds a Markdown note.
* ``sys_workflow_request_input`` asks the user about a task and waits for the
  answer, marking the task ``needs_input`` meanwhile.
* ``sys_workflow_save`` saves a reusable workflow for the user.
* ``sys_workflow_load`` loads one of the user's saved workflows.
* ``sys_workflow_list`` lists the user's saved workflows.

All eight are schema-only: the runner sends the journal tools to the server's
``/v1/sessions/{id}/journal`` routes and the workflow tools to
``/v1/workflows``. They make up the ``journal`` tool group, which every agent
has unless its spec sets ``tool_groups.journal: false``.
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
from omnigent.entities.workflow import WORKFLOW_DESCRIPTION_MAX_CHARS, WORKFLOW_NAME_MAX_CHARS
from omnigent.entities.workflow_questions import (
    HEADER_MAX_CHARS,
    OPTION_DESCRIPTION_MAX_CHARS,
    OPTION_LABEL_MAX_CHARS,
    OPTIONS_MAX,
    OPTIONS_MIN,
    QUESTION_MAX_CHARS,
    QUESTIONS_MAX,
)
from omnigent.tools.base import Tool

# The most entries one sys_journal_read returns.
JOURNAL_READ_PAGE_MAX = 100
# The most workflows one sys_workflow_list returns.
WORKFLOW_LIST_PAGE_MAX = 50

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
            "when its work changes. When the plan starts a run of one of the user's saved "
            "workflows, pass its ID as workflow_id, so the workflow's page lists the run; "
            "leave it out when you replan. Returns the plan's version."
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
                "steps": _steps_schema(
                    "Every step of the plan, with unique IDs and no dependency cycles."
                ),
                "workflow_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{32}$",
                    "description": (
                        "Only on the plan that starts a run of a saved workflow: its ID, "
                        "from sys_workflow_load."
                    ),
                },
            },
            ["name", "steps"],
        )


def _steps_schema(description: str) -> dict[str, Any]:
    """The schema of a plan's steps, which a saved workflow's steps share."""
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": PLAN_MAX_STEPS,
        "description": description,
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": _STEP_ID_PATTERN, "description": _STEP_DESC},
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
                    "description": "IDs of steps that must be done before this one starts.",
                },
            },
            "required": ["id", "name", "depends_on"],
            "additionalProperties": False,
        },
    }


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
            "the user: an approval, a decision, a go-ahead they asked for, or how to get "
            "past something you can't clear yourself, such as CI being down. To ask them, "
            "call sys_workflow_request_input instead, which writes it for you; 'failed' when its "
            "own work failed. text says why in a few words, and is required except for "
            "started. Writing started on a done step reopens it. started and done need "
            "every step this one depends on to be done first; otherwise the write fails "
            "with step_not_ready, naming them. A user stopping you isn't a failure: leave "
            "the step started. In an unattended automation or its workers, needs_input is "
            "refused: decide within the user's authorization, or write failed with the "
            "blocker and continue independent work."
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
                        "Why, e.g. 'p95 430 ms', 'Approve the deploy to prod?' or "
                        "'CI is down: retry later, or skip the check?'."
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


class SysWorkflowRequestInputTool(Tool):
    """Ask the user about a task and wait. Runner-dispatched to the journal's input requests."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_workflow_request_input"``."""
        return "sys_workflow_request_input"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Ask the user about a task in your plan and wait for their answer: an approval, "
            "a decision, or a way past something you can't clear yourself. First finish the "
            "ready tasks that don't depend on the answer. It marks the task needs_input "
            "with your first question, puts the questions in the user's Inbox with your "
            "options as buttons (they can also write their own answer), and returns the "
            "answers. Then write the task's next status: started to resume it, done for an "
            "approval given, failed for one refused. Ask everything you need in one call. "
            "In an unattended automation or its workers, this tool and needs_input are "
            "refused. Decide within the user's authorization and record the choice, or "
            "write failed with the blocker and continue independent work."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        option = {
            "type": "object",
            "properties": {
                "label": {"type": "string", "maxLength": OPTION_LABEL_MAX_CHARS},
                "description": {
                    "type": "string",
                    "maxLength": OPTION_DESCRIPTION_MAX_CHARS,
                    "description": "What choosing it means, in a sentence.",
                },
            },
            "required": ["label"],
            "additionalProperties": False,
        }
        question = {
            "type": "object",
            "properties": {
                "question": {"type": "string", "maxLength": QUESTION_MAX_CHARS},
                "header": {
                    "type": "string",
                    "maxLength": HEADER_MAX_CHARS,
                    "description": "A short label for the question, e.g. 'Profiles'.",
                },
                "options": {
                    "type": "array",
                    "items": option,
                    "minItems": OPTIONS_MIN,
                    "maxItems": OPTIONS_MAX,
                },
                "recommended": {
                    "type": "string",
                    "description": "The label of the option you recommend, if any.",
                },
            },
            "required": ["question", "header", "options"],
            "additionalProperties": False,
        }
        return _schema(
            self.name(),
            self.description(),
            {
                "step": {
                    "type": "string",
                    "pattern": _STEP_ID_PATTERN,
                    "description": "The task in the current plan that waits for the answer.",
                },
                "questions": {
                    "type": "array",
                    "items": question,
                    "minItems": 1,
                    "maxItems": QUESTIONS_MAX,
                },
            },
            ["step", "questions"],
        )


class SysWorkflowSaveTool(Tool):
    """Save a reusable workflow. Runner-dispatched to ``POST /v1/workflows``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_workflow_save"``."""
        return "sys_workflow_save"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Save a reusable workflow for the user: from this session's plan, or one they "
            "describe. Load the save-workflow skill first and follow it. Make the steps generic: "
            "a detail that would differ next run (the repo, the PR, the module, a ticket, a "
            "date) comes out of the steps, and the description says what a run needs "
            "('Give it: …'). Use the name the user picked; if they gave none, suggest one and "
            "save once they agree. A workflow drafted from a description waits for the user's "
            "go-ahead: show the draft first, offering to save it now or try it once. Returns the "
            "workflow's id and name, or name_taken when the user already has a workflow with "
            "that name."
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
                    "maxLength": WORKFLOW_NAME_MAX_CHARS,
                    "description": "The name the user picked, e.g. 'Triage flaky tests'.",
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": WORKFLOW_DESCRIPTION_MAX_CHARS,
                    "description": (
                        "What the workflow does and what a run needs, e.g. 'Finds and fixes "
                        "flaky tests. Give it: the test suite.'"
                    ),
                },
                "steps": _steps_schema(
                    "The workflow's steps, written generically, with unique IDs and no "
                    "dependency cycles."
                ),
            },
            ["name", "description", "steps"],
        )


class SysWorkflowLoadTool(Tool):
    """Load a saved workflow. Runner-dispatched to ``GET /v1/workflows``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_workflow_load"``."""
        return "sys_workflow_load"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Load one of the user's saved workflows by ID or name: its name, its description "
            "(what a run needs), and its steps. To run it, follow the workflow skill: write "
            "its steps as your plan with sys_journal_write_plan, keeping their IDs and "
            "dependencies, with workflow_id set to its ID, and fill the run's details into "
            "step names and goals. A name with "
            "no exact match returns workflow_not_found with the user's closest workflow names; "
            "to pick one by what it does instead, list them with sys_workflow_list."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "workflow": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": WORKFLOW_NAME_MAX_CHARS,
                    "description": "The saved workflow's ID, or its exact name.",
                },
            },
            ["workflow"],
        )


class SysWorkflowListTool(Tool):
    """List saved workflows. Runner-dispatched to ``GET /v1/workflows``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_workflow_list"``."""
        return "sys_workflow_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List the user's saved workflows by name: each one's ID, name and the start of its "
            "description, without its steps. Use it when the user asks what workflows they "
            "have, or asks for a saved workflow without its exact name: pick by description, "
            "and ask which they mean if more than one fits. Then load the one you pick with "
            "sys_workflow_load for its whole description and its steps. next_cursor, when "
            "set, pages to the rest."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _schema(
            self.name(),
            self.description(),
            {
                "cursor": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "next_cursor from an earlier sys_workflow_list call.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": WORKFLOW_LIST_PAGE_MAX,
                    "description": "The most workflows to return.",
                },
            },
            [],
        )


JOURNAL_TOOLS: tuple[type[Tool], ...] = (
    SysJournalReadTool,
    SysJournalWritePlanTool,
    SysJournalWriteStatusTool,
    SysJournalWriteNoteTool,
    SysWorkflowRequestInputTool,
    SysWorkflowSaveTool,
    SysWorkflowLoadTool,
    SysWorkflowListTool,
)
