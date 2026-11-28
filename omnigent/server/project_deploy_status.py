"""What this server runs, what's landed but not running yet, and landing drift.

A ticket's commit is its ``approved`` (else ``verified``) SHA, as in
:mod:`omnigent.server.running_commit`. Tickets in the landed stage or a completed stage
whose commit is on the landing branch but not in the running commit wait for a restart.
A ticket in any earlier stage whose commit is already on the landing branch is drift.

A server deployed apart from the landing checkout (for example an app whose code isn't a
git checkout) can't run git on it. It then reports ``landing: "unavailable"`` with the
ticket commits to compare, and the agent's runner, which runs beside the checkout,
classifies them with :func:`classify`.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from omnigent.errors import OmnigentError
from omnigent.server import running_commit
from omnigent.server.project_landing import (
    _DEFAULT_BRANCH,
    CONFIG_KEY,
    GitError,
    _git,
    landed_stage_id,
    refuse,
    resolve_target,
)

_PAGE = 100


def _all_tickets(store: Any, project_id: str, user_id: str | None) -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        page = store.list(project_id, user_id=user_id, after=after, limit=_PAGE)
        tickets.extend(page.get("data", []))
        if not page.get("has_more"):
            return tickets
        after = page.get("last_id")


def _candidates(project: Any, store: Any, user_id: str | None) -> list[dict[str, Any]]:
    """Each ticket with a landing SHA, and whether its stage means it has shipped."""
    stages = store.stages(project.id, user_id=user_id)["stages"]
    landed = landed_stage_id(project.config, stages)
    finished = {stage["id"] for stage in stages if stage.get("is_complete")}
    rows = []
    for ticket in _all_tickets(store, project.id, user_id):
        sha = running_commit.ticket_sha(ticket)
        if sha is None:
            continue
        stage = ticket.get("stage_id") or ""
        rows.append(
            {
                "ticket_id": ticket["id"],
                "title": ticket.get("title") or "",
                "stage_id": stage,
                "sha": sha,
                "shipped": stage in finished or stage == landed,
                "finished": stage in finished,
            }
        )
    return rows


def _row(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: candidate[key] for key in ("ticket_id", "title", "stage_id", "sha")}


def classify(
    checkout: Path, branch: str, running: str | None, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare ticket commits against the landing branch in *checkout* and the running commit.

    :raises GitError: When *checkout* has no such branch.
    """
    tip = _git(checkout, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    # Commits on the branch that the running code doesn't include yet.
    waiting: set[str] | None = None
    if running is not None:
        listed = _git(checkout, "rev-list", f"{running}..{tip}", check=False)
        if listed.returncode == 0:
            waiting = set(listed.stdout.split())
    not_deployed: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["shipped"]:
            if waiting is not None and candidate["sha"] in waiting:
                not_deployed.append(candidate)
        elif running_commit.is_ancestor(str(checkout), candidate["sha"], tip):
            drift.append(_row(candidate))
    return {
        "running_commit": running,
        "branch": branch,
        "tip": tip,
        "restart_needed": running is not None and running != tip,
        "merged_not_deployed": sum(1 for row in not_deployed if row["finished"]),
        "not_deployed": [_row(row) for row in not_deployed],
        "landing_drift": drift,
    }


def _configured_branch(project: Any) -> str:
    raw = (project.config or {}).get(CONFIG_KEY)
    branch = (raw if isinstance(raw, dict) else {}).get("branch")
    return str(branch) if branch else _DEFAULT_BRANCH


def deploy_status(project: Any, store: Any, user_id: str | None) -> dict[str, Any]:
    """The running commit, the landing tip, tickets waiting on a restart, and drift."""
    running = running_commit.running_commit()
    candidates = _candidates(project, store, user_id)
    try:
        target = resolve_target(project.config)
    except OmnigentError as exc:
        return {
            "landing": "unavailable",
            "reason": str(exc),
            "running_commit": running,
            "branch": _configured_branch(project),
            "candidates": candidates,
        }
    status = classify(target.checkout, target.branch, running, candidates)
    return {"landing": "available", **status}


async def project_deploy_status(project: Any, store: Any, user_id: str | None) -> dict[str, Any]:
    """:func:`deploy_status` off the event loop, with git failures as a clear refusal."""
    try:
        return await asyncio.to_thread(deploy_status, project, store, user_id)
    except (GitError, subprocess.SubprocessError) as exc:
        raise refuse(f"Couldn't read the landing branch: {exc}") from exc
