"""Validation and retry correlation for ordinary replies to Codex async questions."""

from __future__ import annotations

import asyncio
import hashlib
import weakref

from pydantic import ValidationError

from omnigent.entities.conversation import AsyncQuestionReply, MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store import ConversationStore

# custom-lint: disable-next=workspace-scoped-cache -- locks only serialize colliding ids
_reply_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def question_reply_lock(session_id: str) -> asyncio.Lock:
    """Serialize question replies on the same session-affine server."""
    lock = _reply_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _reply_locks[session_id] = lock
    return lock


def prepare_question_reply(
    session_id: str, body: SessionEventInput, store: ConversationStore
) -> tuple[SessionEventInput, str | None]:
    """Validate the source, normalize the user text, and find an already delivered reply."""
    try:
        reply = AsyncQuestionReply.model_validate(body.data["async_question_reply"])
    except ValidationError as exc:
        raise OmnigentError("Invalid async question reply", code=ErrorCode.INVALID_INPUT) from exc
    source = store.get_item(session_id, reply.source_item_id)
    if (
        source is None
        or not isinstance(source.data, MessageData)
        or source.data.async_user_input is None
    ):
        raise OmnigentError(
            "The question does not belong to this chat", code=ErrorCode.INVALID_INPUT
        )
    prompt = source.data.async_user_input
    keys = [f"{prompt.call_id}:{index}" for index in range(len(prompt.questions))]
    if reply.cancelled:
        valid = not reply.answers
    else:
        valid = set(reply.answers) == set(keys) and all(
            answer.strip() for answer in reply.answers.values()
        )
    if not valid:
        raise OmnigentError(
            "Answer every question or cancel without answers", code=ErrorCode.INVALID_INPUT
        )
    # ponytail: scan after the question; add a reply index if long histories make this slow.
    cursor = source.id
    while True:
        page = store.list_items(session_id, after=cursor, type="message", limit=100)
        for item in page.data:
            previous = (
                item.data.async_question_reply if isinstance(item.data, MessageData) else None
            )
            if previous is not None and previous.source_item_id == source.id:
                if previous != reply:
                    raise OmnigentError(
                        "This question already has a different reply. Reload this chat.",
                        code=ErrorCode.CONFLICT,
                    )
                return body, item.id
        if not page.has_more or not page.data:
            break
        cursor = page.data[-1].id
    text = (
        "I dismissed these questions without selecting an answer."
        if reply.cancelled
        else "My answers to your questions:"
    )
    if not reply.cancelled:
        text += "\n\n" + "\n\n".join(
            f"{question.title}\n{reply.answers[key]}"
            for key, question in zip(keys, prompt.questions, strict=True)
        )
    stable_id = hashlib.sha256(
        f"omnigent-async-question:{session_id}:{source.id}".encode()
    ).hexdigest()[:32]
    data = {
        **body.data,
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
        "stable_id": stable_id,
        "async_question_reply": reply.model_dump(),
    }
    return body.model_copy(update={"data": data}), None
