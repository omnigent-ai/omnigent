"""Which automations run a saved workflow, read from the prompt their runs send.

``fixtures/workflow_trigger_prompts.json`` holds prompts ``encodeTriggerPrompt`` in
web/src/components/scheduled/triggerTemplates.ts saves for triggers whose instructions
run a saved workflow, for every source in both modes, so these tests check the server
against what the form writes. A web test keeps the file in step with the form.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.server.scheduled.schedule import strip_trigger_header
from omnigent.server.scheduled.workflow_automations import (
    CONTINUED_SESSION_MESSAGE,
    TASK_SESSION_MESSAGE,
    _workflow_reading,
    workflow_session_reuse_error,
)

PROMPTS: dict[str, str] = json.loads(
    (Path(__file__).parent / "fixtures" / "workflow_trigger_prompts.json").read_text()
)
WORKFLOW_RUN = '/workflow Run the saved workflow "Triage" (ID 0123456789abcdef0123456789abcdef).'
SOURCES = ["jira", "slack", "github", "linear", "pagerduty"]


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


@pytest.mark.parametrize(
    "prompt",
    [
        "Summarize overnight activity",
        "/workflow Add retries",
        f"Please {WORKFLOW_RUN}",
        WORKFLOW_RUN.replace("0123456789abcdef0123456789abcdef", "0123"),
        f"{WORKFLOW_RUN}\nOnly bugs",
        # As in JavaScript, a name never spans lines, and only JavaScript's whitespace trims.
        WORKFLOW_RUN.replace('"Triage"', '"Tri\N{LINE SEPARATOR}age"'),
        f"\x1c{WORKFLOW_RUN}",
        # Only the prompt below a trigger header runs.
        f"<!-- omnigent-trigger v1 {json.dumps({'instructions': WORKFLOW_RUN})} -->\n"
        "Summarize overnight activity",
    ],
)
def test_other_prompts_can_continue_a_session(prompt: str) -> None:
    assert workflow_session_reuse_error(prompt, "continue") is None


@pytest.mark.parametrize("session_mode", ["new", "continue"])
@pytest.mark.parametrize("source", SOURCES)
def test_a_workflow_trigger_cant_send_every_match_to_one_task_session(
    source: str, session_mode: str
) -> None:
    task_session = PROMPTS[f"{source}_task_session"]
    per_match = PROMPTS[f"{source}_per_match"]
    # Without its header, as saved before headers, a prompt reads the same.
    for prompt in (task_session, strip_trigger_header(task_session)):
        assert _workflow_reading(prompt) == (True, True)
        assert workflow_session_reuse_error(prompt, session_mode) == TASK_SESSION_MESSAGE
    # Its polling session continues, and each match gets its own session.
    for prompt in (per_match, strip_trigger_header(per_match)):
        assert _workflow_reading(prompt) == (True, False)
        assert workflow_session_reuse_error(prompt, session_mode) is None


def test_a_header_left_stale_by_an_edit_outside_the_form_is_ignored() -> None:
    prompt = PROMPTS["jira_task_session"]
    edited = prompt.replace(f"{WORKFLOW_RUN}\n\nOnly bugs\n", "Triage the ticket.\n")
    assert edited != prompt
    # The header still names the workflow, but the prompt the model gets doesn't.
    assert _workflow_reading(edited) == (False, True)
    assert workflow_session_reuse_error(edited, "continue") is None


def test_reads_instructions_that_keep_the_prompt_from_splitting() -> None:
    prompt = PROMPTS["jira_task_session_unsplittable"]
    for reading_of in (prompt, strip_trigger_header(prompt)):
        assert _workflow_reading(reading_of) == (True, True)
    assert workflow_session_reuse_error(prompt, "continue") == TASK_SESSION_MESSAGE
    # The same layout with other instructions runs no saved workflow.
    assert _workflow_reading(prompt.replace(WORKFLOW_RUN, "Triage the ticket.")) == (False, True)


def _edit_header(prompt: str, edit: Callable[[dict[str, Any]], dict[str, Any]]) -> str:
    body = strip_trigger_header(prompt)
    line = prompt[: len(prompt) - len(body)]
    raw = json.loads(line.removeprefix("<!-- omnigent-trigger v1 ").removesuffix(" -->\n"))
    return f"<!-- omnigent-trigger v1 {json.dumps(edit(raw))} -->\n{body}"


@pytest.mark.parametrize(
    "edit",
    [
        lambda raw: {**raw, "instructions": "Triage the ticket."},
        lambda raw: {key: value for key, value in raw.items() if key != "taskSessionMode"},
        lambda raw: {**raw, "taskSessionMode": "continue"},
    ],
    ids=["other-instructions", "per-match", "task-session"],
)
@pytest.mark.parametrize(
    "name", ["jira_task_session", "jira_per_match", "jira_task_session_unsplittable"]
)
def test_a_header_never_changes_what_the_prompt_runs(
    name: str, edit: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    prompt = PROMPTS[name]
    edited = _edit_header(prompt, edit)
    assert edited != prompt
    assert _workflow_reading(edited) == _workflow_reading(prompt)
