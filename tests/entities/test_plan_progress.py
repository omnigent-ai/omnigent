"""Tests for a plan's progress summary, which mirrors the chat's task preview."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.entities.plan_progress import PLAN_PROGRESS_STEP_NAME_MAX_CHARS, plan_progress
from omnigent.entities.session_journal import JournalEntry

_REVISION = "graph-1"


def _steps(*specs: tuple[str, list[str]] | str) -> list[dict[str, Any]]:
    """Plan steps named after their IDs: ``"explore"`` or ``("work", ["explore"])``."""
    steps = []
    for spec in specs:
        step_id, depends_on = (spec, []) if isinstance(spec, str) else spec
        steps.append({"id": step_id, "name": step_id.title(), "depends_on": depends_on})
    return steps


def _statuses(*writes: tuple[str, str] | tuple[str, str, str]) -> dict[str, JournalEntry]:
    """Each step's newest status, positioned in write order: ``("work", "done")``.

    A third value is the graph revision the status was written under.
    """
    newest: dict[str, JournalEntry] = {}
    for position, write in enumerate(writes, start=1):
        step, status = write[0], write[1]
        revision = write[2] if len(write) == 3 else _REVISION
        newest[step] = JournalEntry(
            id=f"{position:032x}",
            conversation_id="0" * 32,
            position=position,
            created_at=position,
            kind="status",
            step=step,
            scheduled_task_run_id=None,
            content={"status": status, "text": None, "graph_revision": revision},
        )
    return newest


def _step(steps: list[dict[str, Any]], *writes: tuple[str, str]) -> dict[str, str] | None:
    return plan_progress(steps, _statuses(*writes), graph_revision=_REVISION)["step"]


def test_counts_steps_by_their_newest_status() -> None:
    steps = _steps("a", "b", "c", "d", "e")
    statuses = _statuses(("a", "done"), ("b", "failed"), ("c", "started"), ("d", "needs_input"))

    assert plan_progress(steps, statuses, graph_revision=_REVISION) == {
        "total": 5,
        "done": 1,
        "failed": 1,
        "started": 1,
        "needs_input": 1,
        "step": {"name": "D", "state": "needs_input"},
    }


@pytest.mark.parametrize("final_status", [None, "started", "needs_input", "failed", "done"])
def test_the_final_steps_newest_status_decides_whether_the_plan_is_finished(
    final_status: str | None,
) -> None:
    steps = _steps(("report", ["work"]), "explore", ("work", ["explore"]))
    writes = [("report", final_status)] if final_status else []

    step = _step(steps, *writes)

    assert (step is None) is (final_status == "done")


def test_every_independent_branch_must_finish() -> None:
    steps = _steps("explore", ("code", ["explore"]), ("docs", ["explore"]))

    # The agent never marked explore, so it's still the first step that can start.
    assert _step(steps, ("docs", "done")) == {"name": "Explore", "state": "next"}
    assert _step(steps, ("docs", "done"), ("code", "done")) is None


@pytest.mark.parametrize("upstream_status", [None, "started", "needs_input", "failed"])
def test_a_status_written_before_the_final_step_was_done_does_not_reopen_it(
    upstream_status: str | None,
) -> None:
    steps = _steps("work", ("report", ["work"]))
    writes = [("work", upstream_status)] if upstream_status else []

    progress = plan_progress(
        steps, _statuses(*writes, ("report", "done")), graph_revision=_REVISION
    )

    assert progress["step"] is None
    assert progress["failed"] == (1 if upstream_status == "failed" else 0)


def test_a_restart_after_the_final_step_reopens_the_plan() -> None:
    steps = _steps("work", ("report", ["work"]))

    assert _step(steps, ("report", "done"), ("report", "started")) == {
        "name": "Report",
        "state": "started",
    }
    assert _step(steps, ("report", "done"), ("work", "started")) == {
        "name": "Work",
        "state": "started",
    }
    assert _step(steps, ("report", "done"), ("work", "needs_input")) == {
        "name": "Work",
        "state": "needs_input",
    }


def test_a_replan_that_adds_unfinished_work_reopens_the_plan() -> None:
    finished = _statuses(("explore", "done"), ("work", "done"), ("report", "done"))
    appended = _steps("explore", ("work", ["explore"]), ("report", ["work"]), ("ship", ["report"]))
    inserted = _steps(
        "explore", ("work", ["explore"]), ("review", ["work"]), ("report", ["review"])
    )

    assert plan_progress(appended, finished, graph_revision=_REVISION)["step"] == {
        "name": "Ship",
        "state": "next",
    }
    # Inserting a step before a done final step changes the graph after it was done.
    assert plan_progress(inserted, finished, graph_revision="graph-2")["step"] == {
        "name": "Review",
        "state": "next",
    }
    # A plan from before graph revisions can't tell, so the done final step stands.
    assert plan_progress(inserted, finished, graph_revision=None)["step"] is None


def test_names_the_step_waiting_for_the_user_even_while_another_runs() -> None:
    steps = _steps("deploy", "approve")

    assert _step(steps, ("deploy", "started"), ("approve", "needs_input")) == {
        "name": "Approve",
        "state": "needs_input",
    }


def test_names_the_first_step_in_progress_and_counts_the_others() -> None:
    steps = _steps("explore", ("web", ["explore"]), ("api", ["explore"]), ("docs", ["explore"]))
    statuses = _statuses(("explore", "done"), ("docs", "started"), ("web", "started"))

    progress = plan_progress(steps, statuses, graph_revision=_REVISION)

    assert progress["step"] == {"name": "Web", "state": "started"}
    assert progress["started"] == 2


def test_names_the_next_step_between_steps() -> None:
    steps = _steps("profile", ("ship", ["profile"]))

    assert _step(steps) == {"name": "Profile", "state": "next"}
    assert _step(steps, ("profile", "done")) == {"name": "Ship", "state": "next"}


def test_names_the_failure_when_only_steps_it_holds_up_are_left() -> None:
    steps = _steps("fix", ("suite", ["fix"]), ("pr", ["suite"]))

    assert _step(steps, ("fix", "failed")) == {"name": "Fix", "state": "failed"}


def test_a_ready_step_comes_before_a_failure_elsewhere() -> None:
    steps = _steps("fix", ("suite", ["fix"]), "docs")

    assert _step(steps, ("fix", "failed")) == {"name": "Docs", "state": "next"}


def test_ignores_statuses_of_steps_the_plan_no_longer_has() -> None:
    steps = _steps("only")

    progress = plan_progress(
        steps, _statuses(("removed", "done"), ("gone", "started")), graph_revision=_REVISION
    )

    assert progress == {
        "total": 1,
        "done": 0,
        "failed": 0,
        "started": 0,
        "needs_input": 0,
        "step": {"name": "Only", "state": "next"},
    }


def test_shortens_a_long_step_name() -> None:
    steps = [{"id": "long", "name": "x" * 500, "depends_on": []}]

    step = _step(steps, ("long", "started"))

    assert step is not None
    assert len(step["name"]) == PLAN_PROGRESS_STEP_NAME_MAX_CHARS
    assert step["name"].endswith("\N{HORIZONTAL ELLIPSIS}")
