"""Automations that run a saved workflow, read from the prompt their runs send.

Every run of a saved workflow that an automation starts gets its own new session: such
an automation can't continue one session, and a trigger that runs one can't send every
match to one task session. An automation runs a saved workflow when its prompt, or a
trigger's instructions, is the message ``savedWorkflowMessage`` in web/src/lib/workflow.ts
writes, read back as ``parseSavedWorkflowMessage`` there reads it. A trigger's
instructions come from the prompt below its header, split as ``splitTriggerPrompt`` in
web/src/components/scheduled/triggerTemplates.ts splits it. The header itself is left
out: one can disagree with the prompt below it, and only that prompt runs.
"""

from __future__ import annotations

import re

from omnigent.server.scheduled.schedule import (
    TRIGGER_SERVICE_NAMES,
    is_trigger_prompt,
    strip_trigger_header,
)

CONTINUED_SESSION_MESSAGE = (
    "an automation that runs a saved workflow starts each run in a new session; "
    "session_mode must be 'new'"
)
TASK_SESSION_MESSAGE = (
    "a trigger that runs a saved workflow starts a new session for each match, "
    "so it can't send every match to one task session"
)

# Keep in sync with SAVED_WORKFLOW_RUN in web/src/lib/savedWorkflowRun.ts. Like
# JavaScript's ".", the name matches no line break.
_WORKFLOW_RUN_OPENING = (
    r'[/$]workflow Run the saved workflow "[^\n\r\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]+" '
    r"\(ID [0-9a-f]{32}\)\."
)
_SAVED_WORKFLOW_RUN = re.compile(_WORKFLOW_RUN_OPENING + r"(?:\n\n[\s\S]*)?")
# A run opening instructions whose end the prompt doesn't mark.
_SAVED_WORKFLOW_RUN_START = re.compile(_WORKFLOW_RUN_OPENING + r"(?:\n|\Z)")
# What JavaScript's String.prototype.trim removes.
_JS_WHITESPACE = "\t\n\v\f\r \xa0" + "".join(
    map(chr, (0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF))
)

# Keep the text below in sync with POSTING_INSTRUCTIONS, PER_ITEM_HEADINGS,
# TASK_SESSION_HEADINGS and taskSessionPromptOpening in triggerTemplates.ts.
_POSTING_INSTRUCTIONS = (
    "Anything you post for this, such as a comment, reply, message, issue or pull request, "
    'ends with a line "Sent with Omnigent", since it posts with the owner\'s account.'
)
# (instructions heading, posting heading) of a prompt that starts a session per match,
# then of one that sends every match to one task session.
_PER_MATCH_HEADINGS = (
    "\n\nInstructions for each session:\n",
    "\n\nPosting instructions for each session:\n",
)
_TASK_SESSION_HEADINGS = (
    "\n\nInstructions for the task session:\n",
    "\n\nPosting instructions for the task session:\n",
)
_TEMPLATE_PROMPT_STARTS = tuple(
    f"This automation watches {name} " for name in TRIGGER_SERVICE_NAMES
)
_TASK_SESSION_OPENINGS = tuple(
    f"This automation watches {name} and starts one Omnigent session, its task session,"
    for name in TRIGGER_SERVICE_NAMES
)


def workflow_session_reuse_error(prompt: str, session_mode: str) -> str | None:
    """Why an automation with ``prompt`` can't use ``session_mode``, or None if it can.

    A trigger always continues its polling session, so for a trigger only sending every
    match to one task session reuses a session.
    """
    runs_workflow, task_session = _workflow_reading(prompt)
    if not runs_workflow:
        return None
    if is_trigger_prompt(prompt):
        return TASK_SESSION_MESSAGE if task_session else None
    return CONTINUED_SESSION_MESSAGE if session_mode == "continue" else None


def _workflow_reading(prompt: str) -> tuple[bool, bool]:
    """Whether the prompt a run sends runs a saved workflow, and whether it sends every
    trigger match to one task session."""
    body = strip_trigger_header(prompt)
    instructions = _trigger_instructions(body)
    if instructions is None:
        runs_workflow = _instructions_may_start_with_saved_workflow_run(body)
    else:
        runs_workflow = (
            _SAVED_WORKFLOW_RUN.fullmatch(instructions.strip(_JS_WHITESPACE)) is not None
        )
    return runs_workflow, _is_task_session_prompt(body)


def _instructions_may_start_with_saved_workflow_run(prompt: str) -> bool:
    """Whether a template prompt that won't split could have instructions that run a
    saved workflow: one opens after any of its instructions headings.

    The form reads such a prompt's instructions from its header, which can disagree with
    the prompt that runs, so this reads every place the instructions could start.
    """
    instructions_heading, _ = _headings(prompt)
    return any(
        _SAVED_WORKFLOW_RUN_START.match(section.lstrip(_JS_WHITESPACE)) is not None
        for section in prompt.split(instructions_heading)[1:]
    )


def _is_task_session_prompt(prompt: str) -> bool:
    """``isTaskSessionTriggerPrompt``: whether a trigger sends every match to one task session."""
    return strip_trigger_header(prompt).startswith(_TASK_SESSION_OPENINGS)


def _headings(prompt: str) -> tuple[str, str]:
    """``triggerPromptHeadings``: the headings a prompt was rendered with, from its opening."""
    return _TASK_SESSION_HEADINGS if _is_task_session_prompt(prompt) else _PER_MATCH_HEADINGS


def _trigger_instructions(prompt: str) -> str | None:
    """``splitTriggerPrompt(prompt).userPrompt``: a template prompt's instructions, or all
    of any other prompt; None for a template prompt the form wouldn't split."""
    if not prompt.startswith(_TEMPLATE_PROMPT_STARTS):
        return prompt
    instructions_heading, posting_heading = _headings(prompt)
    sections = prompt.split(instructions_heading)
    if len(sections) != 2:
        return None
    manager, instructions = sections
    suffix = f"\n{_POSTING_INSTRUCTIONS}"
    posting_sections = instructions.split(posting_heading)
    if len(posting_sections) == 2:
        # A legacy prompt may contain the new heading inside the user's instructions.
        if instructions.endswith(suffix):
            return None
        user_prompt, posting = posting_sections
    elif len(posting_sections) == 1 and instructions.endswith(suffix):
        user_prompt, posting = instructions[: -len(suffix)], _POSTING_INSTRUCTIONS
    else:
        return None
    # Like the form, read only a layout that renders back to the same prompt.
    if not user_prompt.strip(_JS_WHITESPACE) or prompt != _combine_trigger_prompt(
        user_prompt, f"{manager}{posting_heading}{posting}"
    ):
        return None
    return user_prompt


def _combine_trigger_prompt(user_prompt: str, system_prompt: str) -> str:
    """``combineTriggerPrompt`` for instructions and the template text around them."""
    instructions_heading, posting_heading = _headings(system_prompt)
    sections = system_prompt.split(posting_heading)
    if len(sections) != 2:
        return f"{system_prompt}{instructions_heading}{user_prompt}"
    manager, posting = sections
    separator = "\n" if posting == _POSTING_INSTRUCTIONS else posting_heading
    return f"{manager}{instructions_heading}{user_prompt}{separator}{posting}"
