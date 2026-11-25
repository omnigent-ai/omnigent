"""Release tooling for the Merger: the pushable prefix of landed work, and revert drafts.

The push prefix maps each unpushed commit on the landing branch to a ticket. A commit
belongs to the first ticket head at or after it, where a head is any SHA a ticket's
``approved`` or ``verified`` label ever held. The prefix may end only where every ticket
in it is complete: Verified (or later) with all of its unpushed commits inside, or fully
undone by a later Verified revert named in its ``reverted-by`` label.

A revert draft builds ``Revert "<subject>"`` commits with ``git merge-tree`` and
``git commit-tree`` and creates one new ``ptb/`` branch for them. It writes no worktree
and never moves the landing branch or any existing branch.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.server.project_landing import (
    _SHA,
    APPROVED_LABEL,
    CONFIG_KEY,
    VERIFIED_LABEL,
    GitError,
    _changed_paths,
    _git,
    _is_ancestor,
    refuse,
    resolve_target,
)

REVERTED_BY_LABEL = "reverted-by"
_SHA_LABELS = (APPROVED_LABEL, VERIFIED_LABEL)
_DEFAULT_REMOTE = "origin"
# A ticket approved before its commit existed can't name it; this absorbs clock skew.
_CLOCK_SLACK_S = 3600
_PAGE = 100


@dataclass
class TicketShas:
    """A ticket and every landing-label SHA it has carried."""

    ticket_id: str
    title: str
    stage_id: str
    verified_stage: bool
    shas: set[str] = field(default_factory=set)
    verified: str | None = None
    reverted_by: str | None = None


@dataclass
class _Commit:
    sha: str
    subject: str
    ticket: TicketShas | None = None
    status: str = "unmapped"
    reason: str | None = None
    reverted_by: str | None = None


def _range(checkout: Path, upstream: str, tip: str) -> list[_Commit]:
    out = _git(checkout, "log", "--reverse", "--format=%H%x00%s", f"{upstream}..{tip}").stdout
    commits = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\0")
        commits.append(_Commit(sha=sha, subject=subject))
    return commits


def _attribute(commits: list[_Commit], tickets: list[TicketShas]) -> None:
    """Give each commit the ticket whose head is the first claimed SHA at or after it.

    When several tickets claim one head, the least complete wins, so a claim can never
    make a commit look more verified than one of its tickets says.
    """
    claims: dict[str, list[TicketShas]] = {}
    for ticket in tickets:
        for sha in ticket.shas:
            claims.setdefault(sha, []).append(ticket)
    current: TicketShas | None = None
    for item in reversed(commits):
        claimants = claims.get(item.sha)
        if claimants:
            current = min(claimants, key=lambda ticket: ticket.verified_stage)
        item.ticket = current


def _blob(checkout: Path, commit: str, path: str) -> str | None:
    result = _git(checkout, "rev-parse", "--verify", "-q", f"{commit}:{path}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _revert_problem(
    checkout: Path,
    commits: list[_Commit],
    ticket: TicketShas,
    revert_index: int,
) -> str | None:
    """Why the ticket's commits aren't fully undone at ``commits[revert_index]``, else None.

    Each path the ticket changed must be back to its content before the ticket's first
    commit, unless a Verified commit between the ticket's last commit and the revert
    changed that path too.
    """
    own = [index for index, item in enumerate(commits) if item.ticket is ticket]
    first, last = own[0], own[-1]
    if revert_index <= last:
        return f"its revert `{commits[revert_index].sha[:12]}` comes before its own last commit"
    before = f"{commits[first].sha}^"
    revert = commits[revert_index].sha
    paths = _changed_paths(checkout, before, commits[last].sha)
    later_verified = [
        item.sha
        for item in commits[last + 1 : revert_index]
        if item.ticket is not None and item.ticket.verified_stage
    ]
    touched_later: set[str] = set()
    for sha in later_verified:
        touched_later.update(_changed_paths(checkout, f"{sha}^", sha))
    for path in paths:
        if _blob(checkout, revert, path) != _blob(checkout, before, path) and (
            path not in touched_later
        ):
            return f"`{path}` isn't restored by the revert `{revert[:12]}`"
    return None


def push_prefix(
    checkout: Path, branch: str, upstream_ref: str, tickets: list[TicketShas]
) -> dict[str, Any]:
    """The longest prefix of ``upstream_ref..branch`` that may be pushed, and why it stops."""
    tip = _git(checkout, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    upstream = _git(checkout, "rev-parse", "--verify", upstream_ref).stdout.strip()
    if not _is_ancestor(checkout, upstream, tip):
        raise refuse(
            f"`{branch}` (`{tip[:12]}`) doesn't fast-forward `{upstream_ref}` "
            f"(`{upstream[:12]}`): a push would not be a fast-forward"
        )
    commits = _range(checkout, upstream, tip)
    _attribute(commits, tickets)
    index_of = {item.sha: index for index, item in enumerate(commits)}
    last_index: dict[str, int] = {}
    for index, item in enumerate(commits):
        if item.ticket is not None:
            last_index[item.ticket.ticket_id] = index
    revert_problems: dict[str, str | None] = {}
    for item in commits:
        ticket = item.ticket
        if ticket is None:
            item.reason = (
                f"`{item.sha[:12]}` isn't any ticket's approved or verified SHA, and no "
                "ticket head follows it"
            )
            continue
        if ticket.verified_stage:
            item.status = "verified"
            if ticket.verified and ticket.verified not in index_of:
                if not _is_ancestor(checkout, ticket.verified, tip):
                    item.status = "unverified"
                    item.reason = (
                        f"ticket `{ticket.ticket_id}` is Verified for `{ticket.verified[:12]}`, "
                        "which hasn't landed: only part of it is on the branch"
                    )
            continue
        revert = ticket.reverted_by
        revert_ticket = commits[index_of[revert]].ticket if revert in index_of else None
        if revert and revert_ticket is not None and revert_ticket.verified_stage:
            if ticket.ticket_id not in revert_problems:
                revert_problems[ticket.ticket_id] = _revert_problem(
                    checkout, commits, ticket, index_of[revert]
                )
            problem = revert_problems[ticket.ticket_id]
            if problem is None:
                item.status, item.reverted_by = "reverted", revert
                continue
            item.status, item.reason = "unverified", f"ticket `{ticket.ticket_id}`: {problem}"
            continue
        item.status = "unverified"
        item.reason = f"ticket `{ticket.ticket_id}` is in `{ticket.stage_id}`, not Verified" + (
            f"; its `{REVERTED_BY_LABEL}` `{revert[:12]}` isn't a Verified commit in the "
            "unpushed range"
            if revert
            else ""
        )
    blocker = next(
        (index for index, item in enumerate(commits) if item.status in ("unverified", "unmapped")),
        len(commits),
    )
    pushable = -1
    required = -1
    for index in range(blocker):
        item = commits[index]
        assert item.ticket is not None
        required = max(required, last_index[item.ticket.ticket_id])
        if item.reverted_by is not None:
            required = max(required, index_of[item.reverted_by])
        if required <= index:
            pushable = index
    stop = None
    if blocker < len(commits):
        item = commits[blocker]
        stop = {
            "sha": item.sha,
            "ticket_id": item.ticket.ticket_id if item.ticket else None,
            "reason": item.reason,
        }
    by_ticket: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(commits):
        if item.ticket is None:
            continue
        row = by_ticket.setdefault(
            item.ticket.ticket_id,
            {
                "ticket_id": item.ticket.ticket_id,
                "title": item.ticket.title,
                "stage_id": item.ticket.stage_id,
                "verified": item.ticket.verified_stage,
                "commit_count": 0,
                "in_prefix": True,
            },
        )
        row["commit_count"] += 1
        row["in_prefix"] = row["in_prefix"] and index <= pushable
    blocked = [
        row["ticket_id"] for row in by_ticket.values() if row["verified"] and not row["in_prefix"]
    ]
    return {
        "branch": branch,
        "tip": tip,
        "upstream_ref": upstream_ref,
        "upstream": upstream,
        "unpushed_count": len(commits),
        "pushable_count": pushable + 1,
        "pushable_sha": commits[pushable].sha if pushable >= 0 else None,
        "stop": stop,
        "blocked_verified_ticket_ids": blocked,
        "commits": [
            {
                "sha": item.sha,
                "subject": item.subject,
                "ticket_id": item.ticket.ticket_id if item.ticket else None,
                "ticket_title": item.ticket.title if item.ticket else None,
                "stage_id": item.ticket.stage_id if item.ticket else None,
                "status": item.status,
                "reverted_by": item.reverted_by,
                "in_prefix": index <= pushable,
            }
            for index, item in enumerate(commits)
        ],
        "tickets": list(by_ticket.values()),
    }


def upstream_ref(config: dict[str, Any], branch: str) -> str:
    raw = config.get(CONFIG_KEY)
    remote = str((raw if isinstance(raw, dict) else {}).get("remote") or _DEFAULT_REMOTE)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", remote) or remote.startswith("-"):
        raise refuse("The landing remote name is invalid")
    return f"refs/remotes/{remote}/{branch}"


def verified_stage_ids(stages: list[dict[str, Any]]) -> set[str]:
    """The Verified stage and every stage after it; the last two when none is named so."""
    names = [str(stage.get("name") or "").strip().casefold() for stage in stages]
    start = names.index("verified") if "verified" in names else max(len(stages) - 2, 0)
    return {stage["id"] for stage in stages[start:]}


def _label_shas(labels: Any) -> set[str]:
    shas = set()
    for label in labels if isinstance(labels, list) else []:
        if isinstance(label, dict) and label.get("key") in _SHA_LABELS:
            value = (label.get("value") or "").strip().lower()
            if _SHA.fullmatch(value):
                shas.add(value)
    return shas


def _label_value(ticket: dict[str, Any], key: str) -> str | None:
    for label in ticket.get("labels") or []:
        if label.get("key") == key:
            value = (label.get("value") or "").strip().lower()
            return value if _SHA.fullmatch(value) else None
    return None


def collect_tickets(
    store: Any, project_id: str, user_id: str | None, since: float
) -> list[TicketShas]:
    """Every ticket touched since *since*, with the landing SHAs its labels ever held."""
    stages = store.stages(project_id, user_id=user_id)["stages"]
    done = verified_stage_ids(stages)
    found: list[TicketShas] = []
    after: str | None = None
    while True:
        page = store.list(project_id, user_id=user_id, after=after, limit=_PAGE)
        for ticket in page.get("data", []):
            if (ticket.get("updated_at") or 0) < since:
                continue
            row = TicketShas(
                ticket_id=ticket["id"],
                title=ticket.get("title") or "",
                stage_id=ticket.get("stage_id") or "",
                verified_stage=ticket.get("stage_id") in done,
                shas=_label_shas(ticket.get("labels")),
                verified=_label_value(ticket, VERIFIED_LABEL),
                reverted_by=_label_value(ticket, REVERTED_BY_LABEL),
            )
            position = -1
            for _ in range(50):
                events = store.events(
                    project_id, ticket["id"], user_id=user_id, after=position, limit=_PAGE
                )
                for event in events.get("data", []):
                    payload = event.get("payload") or {}
                    row.shas |= _label_shas((payload.get("changes") or {}).get("labels"))
                    if event.get("kind") == "created":
                        row.shas |= _label_shas(payload.get("labels"))
                if not events.get("has_more"):
                    break
                position = events.get("last_position", position)
            if row.shas:
                found.append(row)
        if not page.get("has_more"):
            return found
        after = page.get("last_id")


def _oldest_commit_time(checkout: Path, upstream: str, tip: str) -> float | None:
    out = _git(checkout, "log", "--format=%ct", f"{upstream}..{tip}").stdout.split()
    return float(min(int(value) for value in out)) if out else None


async def project_push_prefix(project: Any, store: Any, user_id: str | None) -> dict[str, Any]:
    """The push prefix for *project*'s landing branch, from its tickets' labels."""
    target = await asyncio.to_thread(resolve_target, project.config)
    ref = upstream_ref(project.config, target.branch)

    def compute() -> dict[str, Any]:
        tip = f"refs/heads/{target.branch}"
        if _git(target.checkout, "rev-parse", "--verify", "-q", ref, check=False).returncode:
            raise refuse(f"`{ref}` doesn't exist in the landing checkout: fetch it first")
        oldest = _oldest_commit_time(target.checkout, ref, tip)
        tickets = (
            []
            if oldest is None
            else collect_tickets(store, project.id, user_id, oldest - _CLOCK_SLACK_S)
        )
        return push_prefix(target.checkout, target.branch, ref, tickets)

    try:
        return await asyncio.to_thread(compute)
    except (GitError, subprocess.SubprocessError) as exc:
        raise refuse(f"Couldn't read the landing branch: {exc}") from exc


# -- revert drafts --------------------------------------------------------------


def _revert_message(checkout: Path, sha: str) -> str:
    subject = _git(checkout, "log", "-1", "--format=%s", sha).stdout.strip()
    trailers = _git(checkout, "log", "-1", "--format=%(trailers:only,unfold)", sha).stdout.strip()
    message = f'Revert "{subject}"\n\nThis reverts commit {sha}.\n'
    return message + (f"\n{trailers}\n" if trailers else "")


def draft_revert(checkout: Path, branch: str, shas: list[str], new_branch: str) -> dict[str, Any]:
    """Revert *shas* (landed on *branch*) newest first onto its tip, on *new_branch*.

    Nothing is written but objects and the one new ref; a conflict writes no ref and
    reports the files and the later commits that touched them.
    """
    tip = _git(checkout, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    for sha in shas:
        if _git(checkout, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
            raise refuse(f"`{sha[:12]}` isn't a commit in this repository")
        if not _is_ancestor(checkout, sha, tip):
            raise refuse(f"`{sha[:12]}` hasn't landed on `{branch}`")
        if len(_git(checkout, "rev-list", "--parents", "-1", sha).stdout.split()) != 2:
            raise refuse(
                f"`{sha[:12]}` isn't a single-parent commit, so it can't be reverted here"
            )
    order = _git(checkout, "rev-list", tip).stdout.split()
    position = {sha: index for index, sha in enumerate(order)}
    newest_first = sorted(set(shas), key=lambda sha: position[sha])
    if _git(checkout, "check-ref-format", f"refs/heads/{new_branch}", check=False).returncode:
        raise refuse(f"`{new_branch}` isn't a valid branch name")
    if not _git(
        checkout, "rev-parse", "--verify", "-q", f"refs/heads/{new_branch}", check=False
    ).returncode:
        raise refuse(f"`{new_branch}` already exists; pick another name")
    head = tip
    drafted: list[dict[str, str]] = []
    for sha in newest_first:
        merged = _git(
            checkout,
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            f"--merge-base={sha}",
            head,
            f"{sha}^",
            check=False,
        )
        lines = merged.stdout.splitlines()
        if merged.returncode not in (0, 1) or not lines:
            raise GitError(f"git merge-tree failed: {merged.stderr.strip()[:300]}")
        if merged.returncode == 1:
            files = sorted({line for line in lines[1:] if line})
            later = _git(
                checkout,
                "log",
                "--format=%H%x00%s",
                f"{sha}..{tip}",
                "--",
                *(f":(literal){path}" for path in files),
            ).stdout.splitlines()
            return {
                "ok": False,
                "base": tip,
                "conflict": {
                    "sha": sha,
                    "files": files,
                    "later_commits": [
                        {"sha": entry.partition("\0")[0], "subject": entry.partition("\0")[2]}
                        for entry in later
                    ],
                },
                "drafted": drafted,
            }
        committer = _git(checkout, "log", "-1", "--format=%cn%x00%ce", sha).stdout.strip()
        name, _, email = committer.partition("\0")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        result = subprocess.run(
            ["git", "-C", str(checkout), "commit-tree", lines[0], "-p", head, "-F", "-"],
            input=_revert_message(checkout, sha),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            raise GitError(f"git commit-tree failed: {result.stderr.strip()[:300]}")
        head = result.stdout.strip()
        drafted.append({"reverts": sha, "sha": head})
    # An empty old value makes update-ref refuse to overwrite a branch created meanwhile.
    _git(checkout, "update-ref", f"refs/heads/{new_branch}", head, "")
    return {"ok": True, "base": tip, "branch": new_branch, "head": head, "drafted": drafted}


async def project_revert_draft(
    project: Any, store: Any, user_id: str | None, ticket_id: str, shas: list[str] | None
) -> dict[str, Any]:
    """Draft a revert of a landed ticket's commits on a new ``ptb/revert-…`` branch."""
    target = await asyncio.to_thread(resolve_target, project.config)
    if not shas:
        prefix = await project_push_prefix(project, store, user_id)
        shas = [row["sha"] for row in prefix["commits"] if row["ticket_id"] == ticket_id]
        if not shas:
            raise refuse(
                "None of the ticket's commits are in the unpushed range; pass `shas` with "
                "the landed commits to revert"
            )
    shas = [sha.strip().lower() for sha in shas]
    if any(not _SHA.fullmatch(sha) for sha in shas):
        raise refuse("Give full 40-character commit SHAs")
    new_branch = f"ptb/revert-{ticket_id[:8]}-{shas[-1][:8]}"
    try:
        result = await asyncio.to_thread(
            draft_revert, target.checkout, target.branch, shas, new_branch
        )
    except (GitError, subprocess.SubprocessError) as exc:
        raise refuse(f"Couldn't draft the revert: {exc}") from exc
    if result["ok"]:
        result["next"] = (
            f"Check it out in a new worktree (`git worktree add <path> {new_branch}`), "
            "verify it, and send it for review like any change. Once it is Verified, set "
            f"`{REVERTED_BY_LABEL}` = the revert's head SHA on this ticket."
        )
    return result
