"""Which automations run a saved workflow, read the way the web form reads them.

``fixtures/workflow_trigger_prompts.json`` holds prompts ``encodeTriggerPrompt`` in
web/src/components/scheduled/triggerTemplates.ts saved for triggers whose instructions
run a saved workflow, in Jira's, Slack's and GitHub's layouts, so these tests check the
server against what the form writes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.server.scheduled.schedule import match_trigger_header, strip_trigger_header
from omnigent.server.scheduled.workflow_automations import (
    CONTINUED_SESSION_MESSAGE,
    TASK_SESSION_MESSAGE,
    _trigger_fingerprint,
    _trigger_header,
    _workflow_reading,
    workflow_session_reuse_error,
)

PROMPTS: dict[str, str] = json.loads(
    (Path(__file__).parent / "fixtures" / "workflow_trigger_prompts.json").read_text()
)
WORKFLOW_RUN = '/workflow Run the saved workflow "Triage" (ID 0123456789abcdef0123456789abcdef).'
SOURCES = ["jira", "slack", "github"]


@pytest.mark.parametrize("session_mode", ["new", "continue"])
@pytest.mark.parametrize(
    "prompt",
    [
        WORKFLOW_RUN,
        f"{WORKFLOW_RUN}\n\nOnly bugs",
        f"${WORKFLOW_RUN[1:]}",
        # JavaScript's trim drops a byte order mark too.
        f"﻿ {WORKFLOW_RUN}\n",
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
        WORKFLOW_RUN.replace('"Triage"', '"Tri age"'),
        f"\x1c{WORKFLOW_RUN}",
    ],
)
def test_other_prompts_can_continue_a_session(prompt: str) -> None:
    assert workflow_session_reuse_error(prompt, "continue") is None


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_trusts_the_header_the_form_saved(name: str) -> None:
    prompt = PROMPTS[name]
    header = _trigger_header(prompt)
    assert header is not None
    config, fingerprint = header
    assert fingerprint == _trigger_fingerprint(config, strip_trigger_header(prompt))


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


def test_a_trusted_header_reads_instructions_that_keep_the_prompt_from_splitting() -> None:
    prompt = PROMPTS["jira_task_session_unsplittable"]
    assert _workflow_reading(strip_trigger_header(prompt)) == (False, True)
    assert _workflow_reading(prompt) == (True, True)
    assert workflow_session_reuse_error(prompt, "continue") == TASK_SESSION_MESSAGE
    assert _workflow_reading(prompt.replace("Each run:", "Each run :")) == (False, True)


def _edit_header(prompt: str, edit: Callable[[dict[str, Any]], object], version: str = "1") -> str:
    match = match_trigger_header(prompt)
    assert match is not None
    header = json.dumps(edit(json.loads(match.group(2))))
    return f"<!-- omnigent-trigger v{version} {header} -->\n{prompt[match.end() :]}"


@pytest.mark.parametrize(
    "edit,version,trusted",
    [
        (lambda raw: dict(reversed(raw.items())), "1", True),
        (lambda raw: raw, "01", True),
        (lambda raw: raw, "2", False),
        (lambda raw: {**raw, "taskSessionMode": "sometimes"}, "1", False),
        (lambda raw: {**raw, "extra": float("nan")}, "1", False),
        (lambda raw: [raw], "1", False),
    ],
    ids=["reordered", "padded-version", "newer-version", "bad-mode", "nan", "not-an-object"],
)
def test_trusts_a_header_only_where_the_form_does(
    edit: Callable[[dict[str, Any]], object], version: str, trusted: bool
) -> None:
    prompt = _edit_header(PROMPTS["jira_task_session_unsplittable"], edit, version)
    assert _workflow_reading(prompt) == (trusted, True)
