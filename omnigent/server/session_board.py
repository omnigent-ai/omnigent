"""Server-owned acceptance stamps for the web session Board.

The Board treats a session as accepted while its acceptance label is present
and newer than the session's ``updated_at``. The client sends the
``updated_at`` it reviewed; the store writes the label only if that is still
current, under the conversation's write lock. Every store write that later
advances ``updated_at`` drops the label in its own transaction, so later
activity reopens the work whatever timestamp its writer sampled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import BOARD_ACCEPTED_LABEL_KEY

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

# Writes sampled in the reviewed second may commit just before Accept with that
# same updated_at; refusing a too-recent review narrows that window.
_SETTLE_SECONDS = 2
_CHANGED_MESSAGE = "The session changed since you reviewed it. Review it again before accepting."

__all__ = [
    "BOARD_ACCEPTED_LABEL_KEY",
    "BoardAcceptance",
    "drop_board_acceptance_seed",
    "resolve_board_acceptance",
    "write_board_acceptance",
]


@dataclass(frozen=True)
class BoardAcceptance:
    """A validated Accept (``reviewed`` set) or Reopen (``reviewed`` is ``None``)."""

    reviewed: int | None
    stamp: str


def drop_board_acceptance_seed(labels: dict[str, str] | None) -> None:
    """
    Discard an acceptance label from a new session's labels, in place.

    Native replacement sessions copy their predecessor's labels, which may
    carry an acceptance; a new session has had no review, so it starts open.

    :param labels: Client-supplied labels for a new session, or ``None``.
    """
    if labels:
        labels.pop(BOARD_ACCEPTED_LABEL_KEY, None)


async def resolve_board_acceptance(
    conversation_store: ConversationStore,
    session_id: str,
    requested: str,
    *,
    clock: Callable[[], float] | None = None,
) -> BoardAcceptance:
    """
    Validate a client's acceptance label value before any write.

    :param conversation_store: Store used to read the session's current row.
    :param session_id: Session being accepted, e.g. ``"conv_abc123"``.
    :param requested: The reviewed ``updated_at``, e.g. ``"1700000100"``, or
        ``""`` to reopen.
    :param clock: Wall clock in epoch seconds; defaults to :func:`time.time`.
    :returns: The acceptance to write, stamped one second past the review.
    :raises OmnigentError: 400 for a malformed value, 404 for a missing
        session, 409 when the session changed after the reviewed time or that
        activity is under two seconds old.
    """
    if requested == "":
        return BoardAcceptance(reviewed=None, stamp="")
    try:
        reviewed = int(requested)
    except ValueError as exc:
        raise OmnigentError(
            f"{BOARD_ACCEPTED_LABEL_KEY} must be the session's updated_at in epoch seconds",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conversation is None:
        raise OmnigentError(f"Session {session_id!r} not found", code=ErrorCode.NOT_FOUND)
    if conversation.updated_at != reviewed:
        raise OmnigentError(_CHANGED_MESSAGE, code=ErrorCode.CONFLICT)
    if int((clock or time.time)()) < reviewed + _SETTLE_SECONDS:
        raise OmnigentError(
            "The session just changed. Review it again before accepting.",
            code=ErrorCode.CONFLICT,
        )
    return BoardAcceptance(reviewed=reviewed, stamp=str(reviewed + 1))


async def write_board_acceptance(
    conversation_store: ConversationStore,
    session_id: str,
    acceptance: BoardAcceptance,
) -> None:
    """
    Store a resolved acceptance, re-checking the review inside the write.

    :param conversation_store: Store to write through.
    :param session_id: Session being accepted or reopened.
    :param acceptance: The value from :func:`resolve_board_acceptance`.
    :raises OmnigentError: 409 when the session changed after validation.
    """
    if acceptance.reviewed is None:
        await asyncio.to_thread(
            conversation_store.set_labels, session_id, {BOARD_ACCEPTED_LABEL_KEY: ""}
        )
        return
    accepted = await asyncio.to_thread(
        conversation_store.accept_board_review, session_id, acceptance.reviewed, acceptance.stamp
    )
    if not accepted:
        raise OmnigentError(_CHANGED_MESSAGE, code=ErrorCode.CONFLICT)
