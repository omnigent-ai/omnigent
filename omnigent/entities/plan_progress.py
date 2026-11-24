"""Where a session's plan stands, in the few words a session list shows.

The journal store works this out whenever a plan or a step's status changes and
keeps it on the session's journal heads row, so a session list reads it by
primary key instead of reading journals. It follows the chat's task preview:
it names the step waiting for the user, else the step in progress, else the
next one, else a failure that holds up the rest, and names none once the plan
is finished. A started step counts as in progress whether or not the session
is working; the list shows that from the session's own state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from omnigent.entities.session_journal import JournalEntry

#: The longest step name the progress keeps; a list row shows far less.
PLAN_PROGRESS_STEP_NAME_MAX_CHARS = 120

#: Where the named step stands: waiting for the user, in progress, failed and
#: holding up the steps after it, or the next one to start.
PlanProgressStepState = Literal["needs_input", "started", "failed", "next"]

_StepState = Literal["waiting", "ready", "started", "needs_input", "done", "failed"]


def plan_progress(
    steps: Sequence[Mapping[str, Any]],
    statuses: Mapping[str, JournalEntry],
    *,
    graph_revision: str | int | None,
) -> dict[str, Any]:
    """Count the plan's steps by where they stand and name the step it is on.

    :param steps: The current plan's steps in plan order, each with ``id``,
        ``name`` and ``depends_on``.
    :param statuses: Each step's newest status in the current run, by step ID;
        a step with none is left out. Statuses of steps not in the plan are ignored.
    :param graph_revision: The current plan's graph revision, or for a plan from
        before graph revisions the position where its graph began. A done final
        step written under an older graph doesn't finish the plan while a step
        before it isn't done. ``None`` when that isn't known.
    :returns: ``{"total", "done", "failed", "started", "needs_input", "step"}``,
        where step is ``{"name", "state"}`` (:data:`PlanProgressStepState`), or
        ``None`` when the plan is finished.
    """
    states = _step_states(steps, statuses)
    counts = dict.fromkeys(("done", "failed", "started", "needs_input"), 0)
    for state in states.values():
        if state in counts:
            counts[state] += 1
    step = None
    if not _is_complete(steps, statuses, states, graph_revision):
        step = _named_step(steps, states)
    return {"total": len(steps), **counts, "step": step}


def _step_states(
    steps: Sequence[Mapping[str, Any]], statuses: Mapping[str, JournalEntry]
) -> dict[str, _StepState]:
    """Each step's state; one with no status is ready once every step it depends on is done."""
    states: dict[str, _StepState] = {}
    for step in steps:
        entry = statuses.get(step["id"])
        status = entry.content.get("status") if entry is not None else None
        states[step["id"]] = (
            status if status in ("started", "needs_input", "done", "failed") else "waiting"
        )
    for step in steps:
        if states[step["id"]] == "waiting" and all(
            states.get(dependency) == "done" for dependency in step["depends_on"]
        ):
            states[step["id"]] = "ready"
    return states


def _is_complete(
    steps: Sequence[Mapping[str, Any]],
    statuses: Mapping[str, JournalEntry],
    states: Mapping[str, _StepState],
    graph_revision: str | int | None,
) -> bool:
    """Whether every final step is done, with no work reopened before it since.

    A step before a done final step that isn't done reopens the plan when its
    status is newer than the final step's, or when the graph changed after the
    final step was done. Steps the agent never marked don't.
    """
    by_id = {step["id"]: step for step in steps}
    dependencies = {dependency for step in steps for dependency in step["depends_on"]}
    finals = [step for step in steps if step["id"] not in dependencies]
    if not finals:
        return False
    for final in finals:
        if states[final["id"]] != "done":
            return False
        final_status = statuses[final["id"]]
        if isinstance(graph_revision, int):
            graph_changed = graph_revision > final_status.position
        else:
            graph_changed = (
                graph_revision is not None
                and final_status.content.get("graph_revision") != graph_revision
            )
        pending = list(final["depends_on"])
        seen: set[str] = set()
        while pending:
            step_id = pending.pop()
            if step_id in seen or step_id not in by_id:
                continue
            seen.add(step_id)
            upstream = statuses.get(step_id)
            if states[step_id] != "done" and (
                graph_changed
                or (upstream is not None and upstream.position > final_status.position)
            ):
                return False
            pending.extend(by_id[step_id]["depends_on"])
    return True


def _named_step(
    steps: Sequence[Mapping[str, Any]], states: Mapping[str, _StepState]
) -> dict[str, str] | None:
    """The step an unfinished plan is on, as the chat's task preview names it."""
    for wanted in ("needs_input", "started", "ready", "waiting"):
        step = next((step for step in steps if states[step["id"]] == wanted), None)
        if step is None:
            continue
        if wanted in ("needs_input", "started"):
            return _step_ref(step, wanted)
        if wanted == "waiting":
            failed = _failed_upstream(step, steps, states)
            if failed is not None:
                return _step_ref(failed, "failed")
        return _step_ref(step, "next")
    return None


def _failed_upstream(
    step: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    states: Mapping[str, _StepState],
) -> Mapping[str, Any] | None:
    """The nearest failed step that keeps *step* from starting, through steps that can't start."""
    by_id = {other["id"]: other for other in steps}
    pending = list(step["depends_on"])
    seen: set[str] = set()
    while pending:
        step_id = pending.pop(0)
        if step_id in seen or step_id not in by_id:
            continue
        seen.add(step_id)
        if states[step_id] == "failed":
            return by_id[step_id]
        if states[step_id] == "waiting":
            pending.extend(by_id[step_id]["depends_on"])
    return None


def _step_ref(step: Mapping[str, Any], state: PlanProgressStepState) -> dict[str, str]:
    name = step["name"]
    if len(name) > PLAN_PROGRESS_STEP_NAME_MAX_CHARS:
        name = name[: PLAN_PROGRESS_STEP_NAME_MAX_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return {"name": name, "state": state}
