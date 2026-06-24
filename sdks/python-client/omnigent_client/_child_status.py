"""Canonical sub-agent "busy" predicate — the single source of truth shared
by the CLI REPL and SDK drivers.

A parent session's own ``status`` is per-session: a delegating agent fans out
sub-agents and returns to its own prompt, so the parent reads ``idle`` while
its children are still working. To answer *"is anything in this subtree still
working?"* both the CLI (the ``state: N agents running`` badge / ``↓`` menu in
:mod:`omnigent_ui_sdk.terminal`) and SDK rollups
(:meth:`SessionsNamespace.subtree_busy` / :meth:`SessionsChat.tree_busy`) need
one agreed definition of a *single* child being busy. That definition lives
here so the two can't drift.

The predicate mirrors the web ``SubagentsPanel`` ``childStatus`` semantics
(``ap-web/src/shell/SubagentsPanel.tsx``): awaiting input outranks everything
(a sub-agent parked on an elicitation is still mid-turn), then ``launching``,
then the live ``busy`` flag, then any non-terminal ``current_task_status``.

This is deliberately stateless — no linger/debounce. The terminal host wraps it
with a UI-only linger window to stop the badge flickering between a child's
turns; an SDK eval driver wants the un-debounced ground truth instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Task statuses that mean the child's latest run has settled. Shared with the
# terminal host so the CLI's terminal set and this predicate stay in lockstep.
TERMINAL_TASK_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")


def child_session_busy(
    *,
    busy: bool,
    current_task_status: str | None,
    pending_elicitations_count: int = 0,
) -> bool:
    """Return whether a single sub-agent counts as still working.

    Mirrors the web ``SubagentsPanel`` / CLI ``N agents running`` semantics:

    * ``pending_elicitations_count > 0`` — parked on an elicitation it must
      answer first; still mid-turn, so it counts as busy (outranks ``busy``).
    * ``current_task_status == "launching"`` — spawned, not yet running.
    * ``busy`` — the live "session loop is running" flag from the server.
    * any non-terminal ``current_task_status`` (e.g. ``in_progress`` /
      ``queued``) — a busy-flag gap between turns still counts.

    A warm-idle child (loop idle, no in-progress task, no pending input) and a
    terminal one (``completed`` / ``failed`` / ``cancelled``) return ``False``.

    :param busy: ``ChildSessionSummary.busy`` — server status in
        ``("running", "waiting")``.
    :param current_task_status: ``ChildSessionSummary.current_task_status`` —
        ``launching`` / ``in_progress`` / ``completed`` / ``failed`` /
        ``cancelled`` / ``None``.
    :param pending_elicitations_count: number of approvals/prompts blocking the
        child.
    :returns: ``True`` while the child is still working.
    """
    if pending_elicitations_count > 0 or busy or current_task_status == "launching":
        return True
    return current_task_status is not None and current_task_status not in TERMINAL_TASK_STATUSES


def child_summary_busy(summary: Mapping[str, Any]) -> bool:
    """:func:`child_session_busy` applied to a raw ``ChildSessionSummary`` dict
    as returned by :meth:`SessionsNamespace.child_sessions`.

    Tolerates missing / ``None`` fields (e.g. a poll row with no task yet).
    """
    return child_session_busy(
        busy=bool(summary.get("busy")),
        current_task_status=summary.get("current_task_status"),
        pending_elicitations_count=int(summary.get("pending_elicitations_count") or 0),
    )
