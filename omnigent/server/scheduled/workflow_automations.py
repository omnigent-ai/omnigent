"""Automations that run a saved workflow, read the way the web form reads them.

Every run of a saved workflow that an automation starts gets its own new session: such
an automation can't continue one session, and a trigger that runs one can't send every
match to one task session. An automation runs a saved workflow when its prompt, or a
trigger's instructions, is the message ``savedWorkflowMessage`` in web/src/lib/workflow.ts
writes. This mirrors how ``parseSavedWorkflowMessage`` there reads it back: from the
trigger header when ``decodeTriggerPrompt`` trusts it, otherwise from the prompt as
``splitTriggerPrompt`` splits it (both in web/src/components/scheduled/triggerTemplates.ts).
"""

from __future__ import annotations

import array
import json
import re
import sys
from typing import Any

from omnigent.server.scheduled.schedule import (
    TRIGGER_SERVICE_NAMES,
    is_trigger_prompt,
    match_trigger_header,
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

# Keep in sync with SAVED_WORKFLOW_RUN in web/src/lib/savedWorkflowRun.ts. The name
# leaves out the line breaks JavaScript's "." does.
_SAVED_WORKFLOW_RUN = re.compile(
    r'[/$]workflow Run the saved workflow "[^\n\r  ]+" '
    r"\(ID ([0-9a-f]{32})\)\.(?:\n\n[\s\S]*)?"
)
# What JavaScript's String.prototype.trim removes.
_JS_WHITESPACE = "\t\n\v\f\r \xa0                　﻿"

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
# The ``source`` a trigger header names, e.g. "jira".
_TRIGGER_SOURCES = frozenset(name.lower() for name in TRIGGER_SERVICE_NAMES)

_ABSENT = object()
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


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
    """Whether ``prompt`` runs a saved workflow, and sends every match to one task session."""
    body = strip_trigger_header(prompt)
    from_body = (
        _runs_saved_workflow(_trigger_instructions(body)),
        _is_task_session_prompt(prompt),
    )
    header = _trigger_header(prompt)
    if header is None:
        return from_body
    config, fingerprint = header
    from_header = (_runs_saved_workflow(config["instructions"]), "taskSessionMode" in config)
    # The two agree for a prompt the form saved. When they don't, the prompt was edited
    # outside the form, and the form trusts the header only while its fingerprint matches.
    if from_header == from_body or fingerprint != _trigger_fingerprint(config, body):
        return from_body
    return from_header


def _runs_saved_workflow(instructions: str) -> bool:
    return _SAVED_WORKFLOW_RUN.fullmatch(instructions.strip(_JS_WHITESPACE)) is not None


def _is_task_session_prompt(prompt: str) -> bool:
    """``isTaskSessionTriggerPrompt``: whether a trigger sends every match to one task session."""
    return strip_trigger_header(prompt).startswith(_TASK_SESSION_OPENINGS)


def _headings(prompt: str) -> tuple[str, str]:
    """``triggerPromptHeadings``: the headings a prompt was rendered with, from its opening."""
    return _TASK_SESSION_HEADINGS if _is_task_session_prompt(prompt) else _PER_MATCH_HEADINGS


def _trigger_instructions(prompt: str) -> str:
    """``splitTriggerPrompt(prompt).userPrompt``: a template prompt's instructions, else it all."""
    if not prompt.startswith(_TEMPLATE_PROMPT_STARTS):
        return prompt
    instructions_heading, posting_heading = _headings(prompt)
    sections = prompt.split(instructions_heading)
    if len(sections) != 2:
        return prompt
    manager, instructions = sections
    suffix = f"\n{_POSTING_INSTRUCTIONS}"
    posting_sections = instructions.split(posting_heading)
    if len(posting_sections) == 2:
        # A legacy prompt may contain the new heading inside the user's instructions.
        if instructions.endswith(suffix):
            return prompt
        user_prompt, posting = posting_sections
    elif len(posting_sections) == 1 and instructions.endswith(suffix):
        user_prompt, posting = instructions[: -len(suffix)], _POSTING_INSTRUCTIONS
    else:
        return prompt
    # Like the form, read only a layout that renders back to the same prompt.
    if not user_prompt.strip(_JS_WHITESPACE) or prompt != _combine_trigger_prompt(
        user_prompt, f"{manager}{posting_heading}{posting}"
    ):
        return prompt
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


def _reject_constant(constant: str) -> object:
    """Refuse the NaN and Infinity literals Python reads but ``JSON.parse`` doesn't."""
    raise ValueError(f"not JSON: {constant}")


def _trigger_header(prompt: str) -> tuple[dict[str, Any], object] | None:
    """The form state in ``prompt``'s header, keyed as ``canonicalConfig`` keys it, and its
    saved fingerprint; None where ``decodeTriggerPrompt`` can't read the header.

    The form also checks events and conditions against its templates. A header whose
    fingerprint matches was written by the form, so it passed them then.
    """
    match = match_trigger_header(prompt)
    # The form reads the version with Number(), so "01" is version 1 too.
    if match is None or match.group(1).lstrip("0") != "1":
        return None
    try:
        raw = json.loads(match.group(2), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    config = _canonical_config(raw)
    return None if config is None else (config, raw.get("fingerprint"))


def _canonical_config(raw: dict[str, Any]) -> dict[str, Any] | None:
    """``raw`` in ``canonicalConfig``'s key order, or None where ``decodeTriggerPrompt``
    refuses a value's type."""
    source = raw.get("source")
    scope_operator = raw.get("scopeOperator", _ABSENT)
    target = raw.get("target", _ABSENT)
    link_back_touched = raw.get("linkBackTouched", _ABSENT)
    task_session_mode = raw.get("taskSessionMode", _ABSENT)
    conditions = raw.get("conditions")
    worker = _canonical_worker(raw.get("worker"))
    if not (
        isinstance(source, str)
        and source in _TRIGGER_SOURCES
        and isinstance(raw.get("event"), str)
        and isinstance(raw.get("scope"), str)
        and scope_operator in (_ABSENT, "is", "in", "contains")
        and isinstance(conditions, list)
        and (target is _ABSENT or isinstance(target, str))
        and isinstance(raw.get("instructions"), str)
        and isinstance(raw.get("linkBack"), bool)
        and (link_back_touched is _ABSENT or isinstance(link_back_touched, bool))
        and task_session_mode in (_ABSENT, "new", "continue")
        and worker is not None
    ):
        return None
    canonical_conditions = []
    for condition in conditions:
        canonical_condition = _canonical_condition(condition)
        if canonical_condition is None:
            return None
        canonical_conditions.append(canonical_condition)
    config: dict[str, Any] = {"source": source, "event": raw["event"], "scope": raw["scope"]}
    if scope_operator is not _ABSENT:
        config["scopeOperator"] = scope_operator
    config["conditions"] = canonical_conditions
    if target is not _ABSENT:
        config["target"] = target
    config["instructions"] = raw["instructions"]
    config["linkBack"] = raw["linkBack"]
    if link_back_touched is not _ABSENT:
        config["linkBackTouched"] = link_back_touched
    if task_session_mode == "continue":
        config["taskSessionMode"] = task_session_mode
    config["worker"] = worker
    return config


def _canonical_condition(value: object) -> dict[str, Any] | None:
    """A condition as ``decodeCondition`` reads it and ``canonicalConfig`` keys it."""
    if not isinstance(value, dict) or not isinstance(value.get("value"), str):
        return None
    field, operator = value.get("field"), value.get("operator")
    if not isinstance(field, str) or not isinstance(operator, str):
        return None
    condition: dict[str, Any] = {"field": field, "operator": operator, "value": value["value"]}
    name = value.get("name", _ABSENT)
    if isinstance(name, str):
        condition["name"] = name
    elif name is not _ABSENT:
        return None
    return condition


def _canonical_worker(value: object) -> dict[str, Any] | None:
    """The workers' settings as ``decodeWorker`` reads them and ``canonicalConfig`` keys them."""
    if not isinstance(value, dict):
        return None
    agent_id = value.get("agentId", _ABSENT)
    permission_mode = value.get("permissionMode", _ABSENT)
    if not (
        value.get("placement") in ("same_host", "managed")
        and (agent_id is None or isinstance(agent_id, str))
        and isinstance(value.get("model"), str)
        and isinstance(value.get("effort"), str)
        and (permission_mode is _ABSENT or isinstance(permission_mode, str))
    ):
        return None
    worker = {
        "placement": value["placement"],
        "agentId": agent_id,
        "model": value["model"],
        "effort": value["effort"],
    }
    if isinstance(permission_mode, str) and permission_mode:
        worker["permissionMode"] = permission_mode
    return worker


def _trigger_fingerprint(config: dict[str, Any], body: str) -> str:
    """``triggerFingerprint``: cyrb53 over the UTF-16 code units of the config's JSON and body."""
    text = f"{_json_stringify(config)}\n{body}"
    units = array.array("H", text.encode("utf-16-le", "surrogatepass"))
    if sys.byteorder == "big":
        units.byteswap()
    h1, h2 = 0xDEADBEEF, 0x41C6CE57
    # Math.imul keeps the low 32 bits of each product.
    for code in units:
        h1 = ((h1 ^ code) * 2654435761) & 0xFFFFFFFF
        h2 = ((h2 ^ code) * 1597334677) & 0xFFFFFFFF
    h1 = (((h1 ^ (h1 >> 16)) * 2246822507) & 0xFFFFFFFF) ^ (
        ((h2 ^ (h2 >> 13)) * 3266489909) & 0xFFFFFFFF
    )
    h2 = (((h2 ^ (h2 >> 16)) * 2246822507) & 0xFFFFFFFF) ^ (
        ((h1 ^ (h1 >> 13)) * 3266489909) & 0xFFFFFFFF
    )
    return f"{4294967296 * (2097151 & h2) + h1:014x}"


def _json_stringify(value: object) -> str:
    """``JSON.stringify`` of a canonical config, which holds strings, booleans and null."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # JSON.stringify escapes a lone surrogate, which Python writes as it is.
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)
