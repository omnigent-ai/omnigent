"""Which automations run a saved workflow, read from the prompt their runs send.

``fixtures/workflow_trigger_prompts.json`` holds, for every source in both modes, the
layout of a prompt the trigger form in web/src/components/scheduled/triggerTemplates.ts
saves when its instructions run a saved workflow: the opening, then the instructions
between their headings, which is what the server reads. A web test keeps the file in
step with the form. ``fixtures/saved_workflow_run_openings.json`` holds prompts the form
and the server must read alike: some open like a saved workflow's run off its exact
message, the others don't.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.server.scheduled.workflow_automations import (
    CONTINUED_SESSION_INEXACT_MESSAGE,
    CONTINUED_SESSION_MESSAGE,
    TASK_SESSION_MESSAGE,
    _workflow_reading,
    saved_workflow_id,
    workflow_session_reuse_error,
)

PROMPTS: dict[str, str] = json.loads(
    (Path(__file__).parent / "fixtures" / "workflow_trigger_prompts.json").read_text()
)
OPENINGS: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).parent / "fixtures" / "saved_workflow_run_openings.json").read_text()
)
WORKFLOW_RUN = '/workflow Run the saved workflow "Triage" (ID 0123456789abcdef0123456789abcdef).'
SOURCES = ["jira", "slack", "github", "linear", "pagerduty"]


def _header(**state: object) -> str:
    """A trigger form header line holding *state*."""
    return f"<!-- omnigent-trigger v1 {json.dumps(state)} -->\n"


@pytest.mark.parametrize("session_mode", ["new", "continue"])
@pytest.mark.parametrize(
    "prompt",
    [
        WORKFLOW_RUN,
        f"{WORKFLOW_RUN}\n\nOnly bugs",
        f"${WORKFLOW_RUN[1:]}",
        # JavaScript's trim drops a byte order mark too.
        f"\N{ZERO WIDTH NO-BREAK SPACE} {WORKFLOW_RUN}\n",
    ],
)
def test_a_schedule_that_runs_a_saved_workflow_cant_continue_a_session(
    prompt: str, session_mode: str
) -> None:
    expected = CONTINUED_SESSION_MESSAGE if session_mode == "continue" else None
    assert workflow_session_reuse_error(prompt, session_mode) == expected
    # Each of its runs starts as a run of that workflow.
    assert saved_workflow_id(prompt) == "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "prompt", list(OPENINGS["inexactRuns"].values()), ids=list(OPENINGS["inexactRuns"])
)
def test_a_schedule_whose_prompt_opens_like_a_workflow_run_cant_continue_a_session(
    prompt: str,
) -> None:
    # The agent still runs the workflow; only the exact message starts each run as a run.
    assert workflow_session_reuse_error(prompt, "continue") == CONTINUED_SESSION_INEXACT_MESSAGE
    assert workflow_session_reuse_error(prompt, "new") is None
    assert saved_workflow_id(prompt) is None


@pytest.mark.parametrize(
    "prompt",
    [
        f"Please {WORKFLOW_RUN}",
        # Only JavaScript's whitespace trims.
        f"\x1c{WORKFLOW_RUN}",
        # Only the prompt below a trigger header runs.
        f"{_header(instructions=WORKFLOW_RUN)}Summarize overnight activity",
        *OPENINGS["notRuns"].values(),
    ],
)
def test_other_prompts_can_continue_a_session(prompt: str) -> None:
    assert workflow_session_reuse_error(prompt, "continue") is None
    assert saved_workflow_id(prompt) is None


@pytest.mark.parametrize("session_mode", ["new", "continue"])
@pytest.mark.parametrize("source", SOURCES)
def test_a_workflow_trigger_cant_send_every_match_to_one_task_session(
    source: str, session_mode: str
) -> None:
    task_session = PROMPTS[f"{source}_task_session"]
    per_match = PROMPTS[f"{source}_per_match"]
    # A header line above the prompt changes nothing.
    for prompt in (task_session, _header(source=source) + task_session):
        assert _workflow_reading(prompt) == (True, True)
        assert workflow_session_reuse_error(prompt, session_mode) == TASK_SESSION_MESSAGE
    # Instructions off the exact message still run the workflow.
    inexact = task_session.replace(WORKFLOW_RUN, WORKFLOW_RUN.lower())
    assert workflow_session_reuse_error(inexact, session_mode) == TASK_SESSION_MESSAGE
    # Its polling session continues, and each match gets its own session.
    for prompt in (per_match, _header(source=source) + per_match):
        assert _workflow_reading(prompt) == (True, False)
        assert workflow_session_reuse_error(prompt, session_mode) is None
    # The sessions it starts for its matches run the workflow, not the trigger's own.
    for prompt in (task_session, per_match):
        assert saved_workflow_id(prompt) is None


def test_reads_instructions_that_keep_the_prompt_from_splitting() -> None:
    prompt = PROMPTS["jira_task_session_unsplittable"]
    assert _workflow_reading(prompt) == (True, True)
    assert workflow_session_reuse_error(prompt, "continue") == TASK_SESSION_MESSAGE
    # The same layout with other instructions runs no saved workflow.
    assert _workflow_reading(prompt.replace(WORKFLOW_RUN, "Triage the ticket.")) == (False, True)
    assert _workflow_reading(prompt.replace(WORKFLOW_RUN, WORKFLOW_RUN.lower())) == (True, True)


@pytest.mark.parametrize(
    "header",
    [
        # What the form saved before the prompt was edited outside it.
        _header(instructions=WORKFLOW_RUN, taskSessionMode="continue"),
        _header(instructions="Triage the ticket."),
        _header(instructions="Triage the ticket.", taskSessionMode="continue"),
    ],
    ids=["names-the-workflow", "other-instructions", "other-instructions-task-session"],
)
@pytest.mark.parametrize(
    "prompt",
    [
        PROMPTS["jira_task_session"],
        PROMPTS["jira_per_match"],
        PROMPTS["jira_task_session_unsplittable"],
        PROMPTS["jira_task_session"].replace(f"{WORKFLOW_RUN}\n\nOnly bugs", "Triage the ticket."),
    ],
    ids=["task-session", "per-match", "unsplittable", "without-the-workflow"],
)
def test_a_header_never_changes_what_the_prompt_runs(prompt: str, header: str) -> None:
    assert _workflow_reading(header + prompt) == _workflow_reading(prompt)
