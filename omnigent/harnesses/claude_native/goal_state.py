"""Derive Claude Code's ``/goal`` state from its session transcript.

Claude Code records each goal change as a ``goal_status`` attachment: a
``sentinel`` entry when ``/goal`` sets (``met: false``) or clears (``met: true``)
the goal, then one entry per evaluator check (``met`` / ``failed`` plus the
evaluator's ``reason``). When an interruption stops it from continuing, it
records an informational ``system`` notice such as ``"Goal paused · …"``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

ClaudeGoalStatus = Literal["active", "paused", "complete", "failed"]

_PAUSED_NOTICE_PREFIX = "Goal paused"
_STILL_ACTIVE_NOTICE_PREFIX = "Goal still active"
# Lines that can change goal state; everything else is skipped before parsing JSON.
_RELEVANT_MARKERS = (b'"goal_status"', b'"informational"', b'"type":"user"', b'"type": "user"')


@dataclasses.dataclass(frozen=True)
class ClaudeGoal:
    """
    The goal Claude Code is pursuing, or the one it just finished.

    :param condition: Completion condition from ``/goal``, e.g. ``"tests pass"``.
    :param status: ``"active"``, ``"paused"``, ``"complete"`` or ``"failed"``.
    :param detail: Latest evaluator reason or pause notice, e.g.
        ``"Goal paused · the request was rate limited · send a message to retry"``.
    :param iterations: Evaluator checks Claude reported at completion, e.g. ``3``.
    :param tokens: Tokens Claude reported spending on the goal, e.g. ``1200``.
    :param duration_ms: Time Claude reported spending on the goal, e.g. ``95000``.
    :param set_at: Unix time the goal was set, e.g. ``1779922393.2``.
    :param updated_at: Unix time of the latest goal record, e.g. ``1779922400.0``.
    """

    condition: str
    status: ClaudeGoalStatus
    detail: str | None = None
    iterations: int | None = None
    tokens: int | None = None
    duration_ms: int | None = None
    set_at: float | None = None
    updated_at: float | None = None

    def to_wire(self) -> dict[str, Any]:
        """Return the snake-case JSON shape the runner sends the server."""
        return dataclasses.asdict(self)


def _timestamp(entry: dict[str, Any]) -> float | None:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _optional_int(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _is_user_prompt(entry: dict[str, Any]) -> bool:
    """Whether *entry* is a prompt the user sent (not a tool result or framework text)."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "text" for block in content
        ) and not any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
    return False


def apply_goal_entry(goal: ClaudeGoal | None, entry: dict[str, Any]) -> ClaudeGoal | None:
    """
    Fold one transcript entry into the current goal state.

    :param goal: State before *entry*, or ``None`` when no goal is shown.
    :param entry: One parsed transcript record.
    :returns: State after *entry*.
    """
    if entry.get("isSidechain"):
        return goal
    at = _timestamp(entry)
    attachment = entry.get("attachment")
    if entry.get("type") == "attachment" and isinstance(attachment, dict):
        if attachment.get("type") != "goal_status":
            return goal
        condition = attachment.get("condition")
        if not isinstance(condition, str) or not condition:
            return goal
        met = attachment.get("met") is True
        if attachment.get("sentinel") is True:
            return None if met else ClaudeGoal(condition, "active", set_at=at, updated_at=at)
        reason = attachment.get("reason")
        status: ClaudeGoalStatus = (
            "complete" if met else "failed" if attachment.get("failed") is True else "active"
        )
        return ClaudeGoal(
            condition=condition,
            status=status,
            detail=reason if isinstance(reason, str) and reason else None,
            iterations=_optional_int(attachment.get("iterations")),
            tokens=_optional_int(attachment.get("tokens")),
            duration_ms=_optional_int(attachment.get("durationMs")),
            set_at=goal.set_at if goal is not None and goal.condition == condition else None,
            updated_at=at,
        )
    if goal is None:
        return None
    if entry.get("type") == "system" and entry.get("subtype") == "informational":
        content = entry.get("content")
        if not isinstance(content, str) or goal.status not in ("active", "paused"):
            return goal
        if content.startswith(_PAUSED_NOTICE_PREFIX):
            return dataclasses.replace(goal, status="paused", detail=content, updated_at=at)
        if content.startswith(_STILL_ACTIVE_NOTICE_PREFIX):
            return dataclasses.replace(goal, status="active", detail=content, updated_at=at)
        return goal
    if _is_user_prompt(entry):
        # A message continues a paused goal; a finished goal stays shown until the next turn.
        if goal.status == "paused":
            return dataclasses.replace(goal, status="active", detail=None, updated_at=at)
        if goal.status in ("complete", "failed"):
            return None
    return goal


class ClaudeGoalStateReader:
    """
    Track the goal recorded in one Claude transcript.

    Each :meth:`read` parses only the complete lines appended since the
    previous read, and starts over when the file is replaced or truncated.

    :param transcript_path: Claude transcript JSONL path, e.g.
        ``"/home/user/.claude/projects/x/session.jsonl"``.
    """

    def __init__(self, transcript_path: Path) -> None:
        self.transcript_path = transcript_path
        self._identity: tuple[int, int] | None = None
        self._scanned_to = 0
        self._goal: ClaudeGoal | None = None

    def read(self) -> ClaudeGoal | None:
        """
        Return the transcript's current goal.

        :returns: The goal, or ``None`` when none is set or the file is unreadable.
        :raises OSError: When the transcript cannot be opened.
        """
        with open(self.transcript_path, "rb") as handle:
            status = os.fstat(handle.fileno())
            identity = (status.st_dev, status.st_ino)
            if identity != self._identity or status.st_size < self._scanned_to:
                self._identity, self._scanned_to, self._goal = identity, 0, None
            handle.seek(self._scanned_to)
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                self._scanned_to += len(line)
                if not any(marker in line for marker in _RELEVANT_MARKERS):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    self._goal = apply_goal_entry(self._goal, entry)
        return self._goal
