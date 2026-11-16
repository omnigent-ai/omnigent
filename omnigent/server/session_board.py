"""Server-owned acceptance for the web session Board.

The Board treats a session as accepted while its acceptance label is present
and newer than the session's ``updated_at``. The client sends the
:func:`~omnigent.stores.conversation_store.board_revision` it reviewed (from
the session list); the store writes the label only if that revision is still
current, under the conversation's write lock. Every store write that later
advances ``updated_at`` drops the label in its own transaction, so later
activity reopens the work whatever timestamp its writer sampled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import BOARD_ACCEPTED_LABEL_KEY, board_revision

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

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
    """A validated Accept (``reviewed_revision`` set) or Reopen (``None``)."""

    reviewed_revision: str | None


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
) -> BoardAcceptance:
    """
    Validate a client's acceptance label value before any write.

    :param conversation_store: Store used to read the session's current row.
    :param session_id: Session being accepted, e.g. ``"conv_abc123"``.
    :param requested: The ``board_revision`` the user reviewed, e.g.
        ``"3f2a9c0d81b4e6f7"``, or ``""`` to reopen.
    :returns: The acceptance to write.
    :raises OmnigentError: 404 for a missing session, 409 when the session
        changed after the reviewed revision.
    """
    if requested == "":
        return BoardAcceptance(reviewed_revision=None)
    conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conversation is None:
        raise OmnigentError(f"Session {session_id!r} not found", code=ErrorCode.NOT_FOUND)
    if board_revision(conversation) != requested:
        raise OmnigentError(_CHANGED_MESSAGE, code=ErrorCode.CONFLICT)
    return BoardAcceptance(reviewed_revision=requested)


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
    if acceptance.reviewed_revision is None:
        await asyncio.to_thread(
            conversation_store.set_labels, session_id, {BOARD_ACCEPTED_LABEL_KEY: ""}
        )
        return
    stamp = await asyncio.to_thread(
        conversation_store.accept_board_review, session_id, acceptance.reviewed_revision
    )
    if stamp is None:
        raise OmnigentError(_CHANGED_MESSAGE, code=ErrorCode.CONFLICT)
