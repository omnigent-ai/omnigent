"""Saved workflows, persisted in the ``workflows`` table.

A :class:`Workflow` is a named plan its owner can run: a run copies its steps
into the session's journal as a plan. Workflows have no versions; an edit
rewrites the row, and each run's journal keeps the plan it ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omnigent.entities.session_journal import (
    PLAN_DESCRIPTION_MAX_CHARS,
    PLAN_NAME_MAX_CHARS,
    InvalidJournalEntryError,
    _line,
    normalize_description,
    normalize_plan,
)

# A run's plan takes the workflow's name, so it follows the plan-name limit.
WORKFLOW_NAME_MAX_CHARS = PLAN_NAME_MAX_CHARS
# Save plan stores the description a planning chat proposed with its plan.
WORKFLOW_DESCRIPTION_MAX_CHARS = PLAN_DESCRIPTION_MAX_CHARS


class InvalidWorkflowError(ValueError):
    """A workflow's fields fail validation; the message says which and why."""


@dataclass(frozen=True)
class Workflow:
    """
    A saved workflow.

    :param id: Workflow ID, a bare 32-character hex UUID.
    :param user_id: The owner, or the reserved ``local`` user in single-user mode.
    :param name: One line, e.g. ``"nightly-build"``; unique per owner.
    :param description: What the workflow is for; a run gives it to the agent
        as the goal.
    :param created_at: Unix epoch seconds when it was saved.
    :param updated_at: Unix epoch seconds of the last write.
    :param steps: The saved steps in the journal plan's format; empty for a draft.
    :param planning_session_id: The owner's conversation for iterating on the plan.
    :param description_source: The journal entry ID of the planning-chat proposal
        Save plan took the description from; ``None`` once it's written otherwise.
    """

    id: str
    user_id: str
    name: str
    description: str
    created_at: int
    updated_at: int
    steps: list[dict[str, Any]]
    planning_session_id: str | None = None
    description_source: str | None = None


def normalize_workflow(
    *, name: object, description: object, steps: object
) -> tuple[str, str, list[dict[str, Any]]]:
    """Check a workflow's fields and return them in their stored form.

    :param name: One line of at most :data:`WORKFLOW_NAME_MAX_CHARS`
        characters; whitespace is collapsed.
    :param description: Non-empty text of at most
        :data:`WORKFLOW_DESCRIPTION_MAX_CHARS` characters; it may span lines.
        The store also enforces its 2,000-byte encoded limit.
    :param steps: Empty for a draft; otherwise checked as the journal checks a plan's: unique
        step IDs, dependencies inside the graph, no cycles, at most 100 steps.
    :returns: ``(name, description, steps)``.
    :raises InvalidWorkflowError: If a field is invalid.
    """
    try:
        workflow_name = _line("name", name, WORKFLOW_NAME_MAX_CHARS)
        workflow_steps = (
            []
            if steps == []
            else normalize_plan(steps, name=workflow_name, source="workflow")["steps"]
        )
        text = normalize_description(description)
    except InvalidJournalEntryError as exc:
        raise InvalidWorkflowError(str(exc)) from exc
    return workflow_name, text, workflow_steps
