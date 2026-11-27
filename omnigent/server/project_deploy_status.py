"""What this server runs, what's landed but not running yet, and landing drift.

A ticket's commit is its ``approved`` (else ``verified``) SHA, as in
:mod:`omnigent.server.running_commit`. Tickets in the landed stage or a completed stage
whose commit is on the landing branch but not in the running commit wait for a restart.
A ticket in any earlier stage whose commit is already on the landing branch is drift.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from omnigent.server import running_commit
from omnigent.server.project_landing import (
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


def _row(ticket: dict[str, Any], sha: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket["id"],
        "title": ticket.get("title") or "",
        "stage_id": ticket.get("stage_id") or "",
        "sha": sha,
    }


def deploy_status(project: Any, store: Any, user_id: str | None) -> dict[str, Any]:
    """The running commit, the landing tip, tickets waiting on a restart, and drift."""
    target = resolve_target(project.config)
    tip = _git(target.checkout, "rev-parse", f"refs/heads/{target.branch}").stdout.strip()
    running = running_commit.running_commit()
    stages = store.stages(project.id, user_id=user_id)["stages"]
    landed = landed_stage_id(project.config, stages)
    finished = {stage["id"] for stage in stages if stage.get("is_complete")}
    shipped_stages = finished | ({landed} if landed else set())
    # Commits on the branch that the running code doesn't include yet.
    waiting: set[str] | None = None
    if running is not None:
        listed = _git(target.checkout, "rev-list", f"{running}..{tip}", check=False)
        if listed.returncode == 0:
            waiting = set(listed.stdout.split())
    not_deployed: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    for ticket in _all_tickets(store, project.id, user_id):
        sha = running_commit.ticket_sha(ticket)
        if sha is None:
            continue
        if ticket.get("stage_id") in shipped_stages:
            if waiting is not None and sha in waiting:
                not_deployed.append(_row(ticket, sha))
        elif running_commit.is_ancestor(str(target.checkout), sha, tip):
            drift.append(_row(ticket, sha))
    merged_not_deployed = [row for row in not_deployed if row["stage_id"] in finished]
    return {
        "running_commit": running,
        "branch": target.branch,
        "tip": tip,
        "restart_needed": running is not None and running != tip,
        "merged_not_deployed": len(merged_not_deployed),
        "not_deployed": not_deployed,
        "landing_drift": drift,
    }


async def project_deploy_status(project: Any, store: Any, user_id: str | None) -> dict[str, Any]:
    """:func:`deploy_status` off the event loop, with git failures as a clear refusal."""
    try:
        return await asyncio.to_thread(deploy_status, project, store, user_id)
    except (GitError, subprocess.SubprocessError) as exc:
        raise refuse(f"Couldn't read the landing branch: {exc}") from exc
