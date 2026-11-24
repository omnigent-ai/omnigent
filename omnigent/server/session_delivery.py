"""Deliver one server-authored user turn into an existing session and wake it."""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.datastructures import Headers

from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import ManagedStartupInputs


async def deliver_to_session(
    app: Any,
    inputs: ManagedStartupInputs,
    session_id: str,
    text: str,
    *,
    user_id: str | None,
    stable_id: str,
    first: bool = False,
    automatic: str = "ticket_update",
) -> bool:
    """
    Queue ``text`` as a durable user turn and wake the session if it is idle.

    :param automatic: What sent the turn; an automatic turn that can't be
        delivered expires instead of showing as undelivered.
    :param stable_id: 32 lowercase hex characters; a retry with the same ID
        dedupes, so a crash after enqueue cannot duplicate the turn.
    :returns: ``True`` when a drain was scheduled or the session is host-backed
        (its host drains queued inputs after reconnecting). ``False`` means the
        session is missing or archived, or nothing will drain the input yet.
    """
    conversation = await asyncio.to_thread(inputs.conversations.get_conversation, session_id)
    if conversation is None or conversation.archived:
        return False
    await inputs.enqueue(
        session_id,
        SessionEventInput(
            type="message",
            data={
                "role": "user",
                "stable_id": stable_id,
                "content": [{"type": "input_text", "text": text}],
            },
        ),
        user_id=user_id,
        headers=Headers(),
        first=first,
        automatic=automatic,
    )
    scheduled = await inputs.deliver_pending(app, session_id)
    return conversation.host_id is not None or scheduled
