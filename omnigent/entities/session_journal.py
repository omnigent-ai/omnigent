"""Entries in a session's journal, and the rules every write follows.

A session's journal is an append-only record of what its agent plans and how
the work goes. Each entry has a kind:

* ``plan``: the workflow graph the session follows. The newest one is the
  session's current plan; its version is its place among the session's plans.
* ``status``: where one plan step stands, ``started``, ``done``,
  ``needs_input`` (waiting for the user) or ``failed``. The newest status for a
  step is its current one.
* ``note``: Markdown the agent keeps for itself and for people, first line
  first.
* ``dispatch``, ``dispatch_result`` and ``plan_status``: written by Omnigent,
  not by agents.

The normalizers here check an entry's fields and return the payload to store.
The journal store runs them on every append, so every write path applies them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

JOURNAL_KINDS: tuple[str, ...] = (
    "status",
    "note",
    "dispatch",
    "dispatch_result",
    "plan",
    "plan_status",
)
# The kinds an agent writes through its tools; Omnigent writes the rest.
AGENT_KINDS: frozenset[str] = frozenset({"status", "note", "plan"})

STEP_STATUSES: tuple[str, ...] = ("started", "done", "needs_input", "failed")
# A step ID is short and stable, e.g. "propose"; it is stored in a column.
STEP_ID_MAX_CHARS = 64
_STEP_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
STATUS_TEXT_MAX_CHARS = 80
NOTE_BODY_MAX_BYTES = 4096
PLAN_MAX_STEPS = 100
PLAN_MAX_RAW_BYTES = 32 * 1024
# A plan's name says its goal in a few words, e.g. "Cut GET /orders p95 by 20%".
PLAN_NAME_MAX_CHARS = 80
STEP_NAME_MAX_CHARS = 80
STEP_GOAL_MAX_CHARS = 300
PLAN_SOURCES: tuple[str, ...] = ("agent", "workflow")
# Names the write that appended an entry, e.g. "tool:<tool call ID>".
IDEMPOTENCY_KEY_MAX_CHARS = 128
# One page of a journal read; the agent's tool reads at most 100 entries at a time.
JOURNAL_PAGE_MAX = 1000


class InvalidJournalEntryError(ValueError):
    """An entry's fields fail validation; the message says which and why."""


class JournalEntryTooLargeError(InvalidJournalEntryError):
    """An entry is over a size limit: a kind's raw limit, or the stored-payload limit.

    :param what: What is too large, e.g. ``"body"``.
    :param size: Its size, in bytes.
    :param limit: The limit, in bytes.
    """

    def __init__(self, what: str, size: int, limit: int) -> None:
        super().__init__(
            f"{what} is {size} bytes, over the {limit}-byte limit; write the detail to a "
            "file and reference it"
        )
        self.size = size
        self.limit = limit


# Statuses that need every step the step depends on to be done first.
STATUSES_NEEDING_DEPENDENCIES: frozenset[str] = frozenset({"started", "done"})


class StepNotReadyError(InvalidJournalEntryError):
    """A step can't start or be done yet: steps it depends on aren't done.

    :param step: The step, e.g. ``"ls-video"``.
    :param waiting: The steps it depends on that aren't done, in plan order.
    """

    def __init__(self, step: str, waiting: list[str]) -> None:
        super().__init__(
            f"{step} waits on {', '.join(waiting)}, not done yet. Write done on each "
            "first, citing the step that covered it if another one did, or replan "
            "without it if it's no longer needed"
        )
        self.step = step
        self.waiting = waiting


@dataclass(frozen=True)
class JournalEntry:
    """One entry in a session's journal.

    :param id: Entry ID, a bare 32-character hex UUID.
    :param conversation_id: The session whose journal holds the entry.
    :param position: Its place in the journal: 0, 1, 2, …
    :param created_at: Unix epoch seconds when it was appended.
    :param kind: One of :data:`JOURNAL_KINDS`, e.g. ``"status"``.
    :param step: The plan step it is about, e.g. ``"propose"``, or ``None``.
    :param scheduled_task_run_id: The automation run the session was serving
        when it was appended, or ``None``.
    :param payload: The kind's fields, e.g. ``{"status": "done", "text": "PR merged"}``.
    """

    id: str
    conversation_id: str
    position: int
    created_at: int
    kind: str
    step: str | None
    scheduled_task_run_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class JournalAppend:
    """The result of an append.

    :param entry: The entry now in the journal.
    :param created: ``False`` when an earlier attempt of the same write had
        already appended *entry*.
    """

    entry: JournalEntry
    created: bool


@dataclass(frozen=True)
class JournalPage:
    """One page of a session's journal, in the order requested.

    :param entries: The entries on this page.
    :param has_more: Whether more entries follow in the same order.
    """

    entries: list[JournalEntry]
    has_more: bool


def _text(name: str, value: object) -> str:
    """Check *value* is valid Unicode text and return it."""
    if not isinstance(value, str):
        raise InvalidJournalEntryError(f"{name} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # e.g. an unpaired surrogate from raw JSON
        raise InvalidJournalEntryError(f"{name} must be valid Unicode text") from exc
    return value


def _line(name: str, value: object, max_chars: int) -> str:
    """Collapse a one-line value's whitespace and bound its length."""
    collapsed = " ".join(_text(name, value).split())
    if not collapsed:
        raise InvalidJournalEntryError(f"{name} must not be empty")
    if len(collapsed) > max_chars:
        raise InvalidJournalEntryError(f"{name} must be at most {max_chars} characters")
    return collapsed


def normalize_step_id(step: object) -> str:
    """Check a step ID and return it.

    :param step: e.g. ``"propose"``: lowercase letters, digits, ``-`` and ``_``,
        starting with a letter or digit, at most :data:`STEP_ID_MAX_CHARS`.
    :raises InvalidJournalEntryError: If it doesn't match that form.
    """
    value = _text("step", step).strip()
    if not _STEP_ID.fullmatch(value):
        raise InvalidJournalEntryError(
            f"step must be a short ID like 'propose': lowercase letters, digits, '-' and "
            f"'_', starting with a letter or digit, at most {STEP_ID_MAX_CHARS} characters"
        )
    return value


def normalize_status(status: object, *, text: object = None) -> dict[str, Any]:
    """Check a step status and return its payload.

    :param status: One of :data:`STEP_STATUSES`.
    :param text: Why, in at most :data:`STATUS_TEXT_MAX_CHARS` characters;
        required except for ``started``.
    :returns: ``{"status": …, "text": … | None}``.
    :raises InvalidJournalEntryError: If a field is invalid.
    """
    if status not in STEP_STATUSES:
        raise InvalidJournalEntryError(f"status must be one of {', '.join(STEP_STATUSES)}")
    if text is None or (isinstance(text, str) and not text.strip()):
        if status != "started":
            raise InvalidJournalEntryError(f"text is required for a {status} status")
        text = None
    else:
        text = _line("text", text, STATUS_TEXT_MAX_CHARS)
    return {"status": status, "text": text}


def normalize_note(body: object) -> dict[str, Any]:
    """Check a note and return its payload.

    :param body: Markdown whose first line is the summary, at most
        :data:`NOTE_BODY_MAX_BYTES` UTF-8 bytes.
    :returns: ``{"body": …}``, without surrounding blank space.
    :raises InvalidJournalEntryError: If it is empty or too large.
    """
    value = _text("body", body).strip()
    if not value:
        raise InvalidJournalEntryError("body must not be empty")
    size = len(value.encode("utf-8"))
    if size > NOTE_BODY_MAX_BYTES:
        raise JournalEntryTooLargeError("body", size, NOTE_BODY_MAX_BYTES)
    return {"body": value}


def _plan_step(raw: object, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidJournalEntryError(f"steps[{index}] must be an object")
    unknown = set(raw) - {"id", "name", "goal", "depends_on"}
    if unknown:
        raise InvalidJournalEntryError(
            f"steps[{index}] has unknown fields: {', '.join(sorted(map(str, unknown)))}"
        )
    step_id = normalize_step_id(raw.get("id"))
    name = _line(f"steps[{index}].name", raw.get("name"), STEP_NAME_MAX_CHARS)
    goal = raw.get("goal")
    if goal is not None and not (isinstance(goal, str) and not goal.strip()):
        goal = _line(f"steps[{index}].goal", goal, STEP_GOAL_MAX_CHARS)
    else:
        goal = None
    depends_on = raw.get("depends_on", [])
    if not isinstance(depends_on, Sequence) or isinstance(depends_on, str):
        raise InvalidJournalEntryError(f"steps[{index}].depends_on must be a list of step IDs")
    dependencies = [normalize_step_id(dependency) for dependency in depends_on]
    if len(set(dependencies)) != len(dependencies):
        raise InvalidJournalEntryError(f"step {step_id!r} lists a dependency twice")
    return {"id": step_id, "name": name, "goal": goal, "depends_on": dependencies}


def _check_acyclic(steps: list[dict[str, Any]]) -> None:
    """Raise if the dependencies form a cycle (Kahn's algorithm)."""
    waiting = {step["id"]: len(step["depends_on"]) for step in steps}
    dependents: dict[str, list[str]] = {step["id"]: [] for step in steps}
    for step in steps:
        for dependency in step["depends_on"]:
            dependents[dependency].append(step["id"])
    ready = [step_id for step_id, count in waiting.items() if count == 0]
    done = 0
    while ready:
        step_id = ready.pop()
        done += 1
        for dependent in dependents[step_id]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                ready.append(dependent)
    if done != len(steps):
        cyclic = sorted(step_id for step_id, count in waiting.items() if count > 0)
        raise InvalidJournalEntryError(
            f"the plan's dependencies form a cycle among: {', '.join(cyclic)}"
        )


def normalize_plan(steps: object, *, name: object, source: str = "agent") -> dict[str, Any]:
    """Check a plan's name and graph and return its payload, without its version.

    :param steps: The whole graph: a list of ``{"id", "name", "goal",
        "depends_on"}`` objects with unique IDs, dependencies on steps in the
        plan, and no cycles; at most :data:`PLAN_MAX_STEPS` steps and
        :data:`PLAN_MAX_RAW_BYTES` of JSON.
    :param name: The plan's goal in a few words, e.g. ``"Cut GET /orders p95
        by 20%"``: one line of at most :data:`PLAN_NAME_MAX_CHARS`.
    :param source: Who drew it, one of :data:`PLAN_SOURCES`.
    :returns: ``{"name", "source", "steps"}``; the store adds ``version``.
    :raises InvalidJournalEntryError: If the name, the graph or a step is invalid.
    """
    if name is None:
        raise InvalidJournalEntryError("a plan needs a name: its goal in a few words")
    plan_name = _line("name", name, PLAN_NAME_MAX_CHARS)
    if source not in PLAN_SOURCES:
        raise InvalidJournalEntryError(f"source must be one of {', '.join(PLAN_SOURCES)}")
    if not isinstance(steps, Sequence) or isinstance(steps, str):
        raise InvalidJournalEntryError("steps must be a list")
    if not steps:
        raise InvalidJournalEntryError("a plan needs at least one step")
    if len(steps) > PLAN_MAX_STEPS:
        raise InvalidJournalEntryError(f"a plan has at most {PLAN_MAX_STEPS} steps")
    normalized = [_plan_step(raw, index) for index, raw in enumerate(steps)]
    ids = [step["id"] for step in normalized]
    duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
    if duplicates:
        raise InvalidJournalEntryError(f"step IDs must be unique: {', '.join(duplicates)}")
    known = set(ids)
    for step in normalized:
        missing = [dependency for dependency in step["depends_on"] if dependency not in known]
        if missing:
            raise InvalidJournalEntryError(
                f"step {step['id']!r} depends on steps not in the plan: {', '.join(missing)}"
            )
    _check_acyclic(normalized)
    payload = {"name": plan_name, "source": source, "steps": normalized}
    size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > PLAN_MAX_RAW_BYTES:
        raise JournalEntryTooLargeError("the plan", size, PLAN_MAX_RAW_BYTES)
    return payload


def normalize_payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Check an entry's payload for its kind and return the payload to store.

    :param kind: One of :data:`JOURNAL_KINDS`.
    :param payload: The kind's fields; for a plan, without ``version``.
    :raises InvalidJournalEntryError: If the kind is unknown, has no writer yet,
        or a field is invalid.
    """
    if kind == "status":
        unknown = set(payload) - {"status", "text"}
        if unknown:
            raise InvalidJournalEntryError(f"unknown status fields: {', '.join(sorted(unknown))}")
        return normalize_status(payload.get("status"), text=payload.get("text"))
    if kind == "note":
        if set(payload) != {"body"}:
            raise InvalidJournalEntryError("a note has exactly one field, body")
        return normalize_note(payload["body"])
    if kind == "plan":
        unknown = set(payload) - {"name", "source", "steps"}
        if unknown:
            raise InvalidJournalEntryError(f"unknown plan fields: {', '.join(sorted(unknown))}")
        return normalize_plan(
            payload.get("steps"),
            name=payload.get("name"),
            source=payload.get("source", "agent"),
        )
    if kind in JOURNAL_KINDS:
        raise InvalidJournalEntryError(f"{kind} entries can't be written yet")
    raise InvalidJournalEntryError(f"kind must be one of {', '.join(JOURNAL_KINDS)}")
