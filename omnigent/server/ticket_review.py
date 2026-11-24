"""Independent review: route a ticket in Review between its worker and the Reviewer persona.

Traffic goes through server-delivered ticket turns, never agent-to-agent messages.
Approval is bound to one commit: only the Reviewer's session may set ``approved``,
and a later commit from anyone else clears it.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.persona_mentions import Mention

# The Review stage's id; this project's "Implemented" stage already uses ``review``.
REVIEW_STAGE_ID = "code_review"
REVIEWER_NAME = "Reviewer"
APPROVED_LABEL = "approved"
RISK_LABEL = "risk"
# Rounds after which the Reviewer is reminded to escalate instead of iterating.
ESCALATE_AFTER_ROUNDS = 3
# Routed turns per ticket per window, so a confused pair can't loop unbounded.
ROUTE_LIMIT = 30
ROUTE_WINDOW_SECONDS = 3600.0
_SHA = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")
_EXCERPT_CHARS = 3000


def find_reviewer(personas: Iterable[Mention]) -> Mention | None:
    """The project's Reviewer identity, matched by name."""
    return next(
        (persona for persona in personas if persona.name.casefold() == REVIEWER_NAME.casefold()),
        None,
    )


def label_value(labels: Iterable[dict[str, Any]] | None, key: str) -> str | None:
    for label in labels or ():
        if label.get("key") == key:
            value = label.get("value")
            return value if isinstance(value, str) else ""
    return None


def is_reviewer_session(conversation: Any, reviewer: Mention | None) -> bool:
    """Whether a session works for the Reviewer persona (by its server-owned label)."""
    if reviewer is None or conversation is None:
        return False
    return (getattr(conversation, "labels", None) or {}).get(
        PROJECT_IDENTITY_LABEL_KEY
    ) == reviewer.id


def check_approval_change(
    before: str | None,
    after: str | None,
    *,
    author_session_id: str | None,
    author_is_reviewer: bool,
) -> None:
    """Refuse setting or changing ``approved`` from anyone but the Reviewer or the owner.

    :raises OmnigentError: ``FORBIDDEN`` for another session, ``INVALID_INPUT``
        unless the value is a full 40-character lowercase commit SHA.
    """
    if after is None or after == before:
        return
    if author_session_id is not None and not author_is_reviewer:
        raise OmnigentError(
            "Only the Reviewer persona's session can approve a change",
            code=ErrorCode.FORBIDDEN,
        )
    if not _SHA.fullmatch(after):
        raise OmnigentError(
            "approved must be the full 40-character lowercase commit SHA",
            code=ErrorCode.INVALID_INPUT,
        )


def superseding_sha(text: str | None, approved: str | None) -> str | None:
    """A commit SHA in a worker's text that differs from the approved one, if any."""
    if not text or not approved:
        return None
    return next((sha for sha in _SHA.findall(text) if sha != approved), None)


class RouteLimiter:
    """Sliding-window cap on routed review turns per ticket."""

    def __init__(self, limit: int = ROUTE_LIMIT, window: float = ROUTE_WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._turns: dict[str, deque[float]] = {}

    def allow(self, ticket_id: str) -> bool:
        now = time.monotonic()
        turns = self._turns.setdefault(ticket_id, deque())
        while turns and now - turns[0] >= self.window:
            turns.popleft()
        if len(turns) >= self.limit:
            return False
        turns.append(now)
        return True


def _quote(text: str) -> str:
    excerpt = text if len(text) <= _EXCERPT_CHARS else text[:_EXCERPT_CHARS] + "…"
    return "\n".join(f"> {line}" for line in excerpt.splitlines() or [""])


_ROUTING_NOTE = (
    "Your ticket comments on it are delivered to the other side automatically while it is "
    "in Review; reply with sys_ticket_comment and don't use sys_session_message."
)


def reviewer_handoff_message(
    ticket: dict[str, Any], move_comment: str | None, worker_session_id: str | None
) -> str:
    """The Reviewer's turn when a ticket enters Review."""
    worker = f"session `{worker_session_id}`" if worker_session_id else "the project owner"
    handoff = _quote(move_comment) if move_comment else "> (no handoff comment)"
    description = _quote(ticket.get("description") or "")
    return (
        f'Review requested on ticket "{ticket["title"]}" (`{ticket["id"]}`) by {worker}.\n\n'
        f"Handoff (branch, SHA, base and tests):\n{handoff}\n\n"
        f"Done-when and context, from the description:\n{description}\n\n"
        "Review it under your persona's rules. Post findings as one ticket comment, each "
        "marked blocking or non-blocking. When nothing blocks, set the label "
        f"`{APPROVED_LABEL}` = the full SHA you reviewed with sys_ticket_update, keeping other "
        f"labels; add `{RISK_LABEL}` = <area> for migrations, auth or permissions, deletion, or "
        f"the Mandate or review rules. {_ROUTING_NOTE}"
    )


def to_reviewer_message(
    ticket: dict[str, Any], text: str, author_session_id: str | None, rounds: int
) -> str:
    """The Reviewer's turn when the worker replies during Review."""
    escalate = (
        f"\n\nThis is round {rounds} without approval. Per your rules, if you still disagree, "
        "escalate: post one comment with audience human that states the disagreement and your "
        "recommendation, then stop iterating."
        if rounds >= ESCALATE_AFTER_ROUNDS
        else ""
    )
    return (
        f'The worker (session `{author_session_id}`) replied on ticket "{ticket["title"]}" '
        f"(`{ticket['id']}`), in Review:\n\n{_quote(text)}\n\n"
        "Re-review only what changed since your last round, plus anything it affects, and "
        f"approve the new SHA if nothing blocks. {_ROUTING_NOTE}{escalate}"
    )


def to_worker_message(ticket: dict[str, Any], text: str) -> str:
    """A ticket worker's turn when the Reviewer posts during Review."""
    return (
        f'The Reviewer commented on your ticket "{ticket["title"]}" (`{ticket["id"]}`), in '
        f"Review:\n\n{_quote(text)}\n\n"
        "Fix blocking findings, or answer with a reason, and rebase to a fast-forward on the "
        "tip. Then post one ticket comment with the new full SHA and what changed. A new SHA "
        f"clears any earlier approval. {_ROUTING_NOTE}"
    )
