"""Saved workflows, persisted in the ``workflows`` table.

A :class:`Workflow` is a named plan its owner can run: a run copies its steps
into the session's journal as a plan. Workflows have no versions; an edit
rewrites the row, and each run's journal keeps the plan it ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omnigent.entities.session_journal import (
    PLAN_NAME_MAX_CHARS,
    InvalidJournalEntryError,
    _line,
    normalize_plan,
)

# A run's plan takes the workflow's name, so it follows the plan-name limit.
WORKFLOW_NAME_MAX_CHARS = PLAN_NAME_MAX_CHARS
WORKFLOW_DESCRIPTION_MAX_CHARS = 2000


class InvalidWorkflowError(ValueError):
    """A workflow's fields fail validation; the message says which and why."""


@dataclass(frozen=True)
class Workflow:
    """
    A saved workflow.

    :param id: Workflow ID, a bare 32-character hex UUID.
    :param user_id: The owner, e.g. ``"alice@example.com"``; ``None`` in
        single-user mode.
    :param name: One line, e.g. ``"nightly-build"``; unique per owner.
    :param description: What the workflow is for; a run gives it to the agent
        as the goal.
    :param created_at: Unix epoch seconds when it was saved.
    :param updated_at: Unix epoch seconds of the last write.
    :param steps: The steps in the journal plan's format, or ``None`` when read
        without them, as a list page reads it.
    """

    id: str
    user_id: str | None
    name: str
    description: str
    created_at: int
    updated_at: int
    steps: list[dict[str, Any]] | None = None


def normalize_workflow(
    *, name: object, description: object, steps: object
) -> tuple[str, str, list[dict[str, Any]]]:
    """Check a workflow's fields and return them in their stored form.

    :param name: One line of at most :data:`WORKFLOW_NAME_MAX_CHARS`
        characters; whitespace is collapsed.
    :param description: Non-empty text of at most
        :data:`WORKFLOW_DESCRIPTION_MAX_CHARS` characters; it may span lines.
    :param steps: The graph, checked as the journal checks a plan's: unique
        step IDs, dependencies inside the graph, no cycles, at most 100 steps.
    :returns: ``(name, description, steps)``.
    :raises InvalidWorkflowError: If a field is invalid.
    """
    try:
        workflow_name = _line("name", name, WORKFLOW_NAME_MAX_CHARS)
        workflow_steps = normalize_plan(steps, name=workflow_name, source="workflow")["steps"]
    except InvalidJournalEntryError as exc:
        raise InvalidWorkflowError(str(exc)) from exc
    if not isinstance(description, str) or not description.strip():
        raise InvalidWorkflowError("description must not be empty")
    text = description.strip()
    if len(text) > WORKFLOW_DESCRIPTION_MAX_CHARS:
        raise InvalidWorkflowError(
            f"description must be at most {WORKFLOW_DESCRIPTION_MAX_CHARS} characters"
        )
    return workflow_name, text, workflow_steps
