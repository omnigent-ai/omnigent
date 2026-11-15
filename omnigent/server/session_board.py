"""Server-owned acceptance stamps for the web session Board.

The Board treats a session as accepted while its acceptance stamp is newer
than the session's ``updated_at``. Activity writers stamp ``updated_at`` with
whole server seconds, so the stamp is one second past the reviewed activity
and is only stored once that second (plus a settle window for writers that
commit after reading the clock) has ended: every later write then has an
``updated_at`` at least as new as the stamp and reopens the work. An Accept
arriving sooner is refused, since a later write in the reviewed second would
be indistinguishable from the reviewed one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import BOARD_ACCEPTED_LABEL_KEY

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

_SETTLE_SECONDS = 2

__all__ = ["BOARD_ACCEPTED_LABEL_KEY", "reject_board_acceptance_seed", "stamp_board_acceptance"]


def reject_board_acceptance_seed(labels: Mapping[str, str] | None) -> None:
    """
    Refuse the acceptance label on session creation, where no review happened.

    :param labels: Client-supplied labels for a new session, or ``None``.
    :raises OmnigentError: 400 when the acceptance label is present.
    """
    if labels and BOARD_ACCEPTED_LABEL_KEY in labels:
        raise OmnigentError(
            f"{BOARD_ACCEPTED_LABEL_KEY} can only be set on an existing session",
            code=ErrorCode.INVALID_INPUT,
        )


async def stamp_board_acceptance(
    conversation_store: ConversationStore,
    session_id: str,
    requested: str,
    *,
    clock: Callable[[], float] | None = None,
) -> str:
    """
    Resolve a client's acceptance label value into the stamp to store.

    The client sends the ``updated_at`` it reviewed; an empty value reopens
    the session and is stored as is.

    :param conversation_store: Store used to read the session's current row.
    :param session_id: Session being accepted, e.g. ``"conv_abc123"``.
    :param requested: Label value from the request, e.g. ``"1700000100"``.
    :param clock: Wall clock in epoch seconds; defaults to :func:`time.time`.
    :returns: ``""`` to reopen, else the stamp to store, e.g. ``"1700000101"``.
    :raises OmnigentError: 400 for a malformed value, 404 for a missing
        session, 409 when the session changed after the reviewed time or that
        activity is under two seconds old.
    """
    if requested == "":
        return ""
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
        raise OmnigentError(
            "The session changed since you reviewed it. Review it again before accepting.",
            code=ErrorCode.CONFLICT,
        )
    # Writers read the clock before they commit, so one can land up to about a
    # second late with an older updated_at; refuse until that window has passed.
    if int((clock or time.time)()) < reviewed + _SETTLE_SECONDS:
        raise OmnigentError(
            "The session just changed. Review it again before accepting.",
            code=ErrorCode.CONFLICT,
        )
    return str(reviewed + 1)
