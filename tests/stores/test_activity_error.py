"""Latest-activity-error label: the fold rule and its maintenance on append."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event

from omnigent.db.db_models import SqlConversationLabel
from omnigent.entities import (
    CompactionData,
    ConversationItem,
    ErrorData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.activity_error import (
    LATEST_ACTIVITY_ERROR_LABEL_KEY,
    fold_activity_error,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_KEY = LATEST_ACTIVITY_ERROR_LABEL_KEY


def _error(code: str = "rate_limit_exceeded", *, response_id: str = "r1") -> NewConversationItem:
    return NewConversationItem(
        type="error",
        response_id=response_id,
        data=ErrorData(source="execution", code=code, message="Rate limited"),
    )


def _info_error(*, response_id: str = "r1") -> NewConversationItem:
    return NewConversationItem(
        type="error",
        response_id=response_id,
        data=ErrorData(
            source="execution", code="codex_new_thread", message="New thread", level="info"
        ),
    )


def _disconnect(response_id: str = "r1") -> NewConversationItem:
    return _error("runner_disconnected", response_id=response_id)


def _text(text: str, *, response_id: str = "r1", is_meta: bool = False) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="assistant",
            agent="claude",
            is_meta=is_meta,
            content=[{"type": "output_text", "text": text}],
        ),
    )


def _user(text: str = "hi", *, response_id: str = "r1") -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _tool_output(*, response_id: str = "r1") -> NewConversationItem:
    return NewConversationItem(
        type="function_call_output",
        response_id=response_id,
        data=FunctionCallOutputData(call_id="c1", output="done"),
    )


def _compaction() -> NewConversationItem:
    return NewConversationItem(
        type="compaction",
        response_id="r1",
        data=CompactionData(summary="s", last_item_id="i0", token_count=1),
    )


def _pending(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=PENDING_INPUT_RESPONSE_ID,
        status="in_progress",
        stable_id=uuid4().hex,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _delivered(pending: ConversationItem) -> NewConversationItem:
    """The native receipt that takes over a pending input's slot."""
    return NewConversationItem(
        type="message",
        response_id="r1",
        stable_id=uuid4().hex,
        replaces_pending_id=pending.id,
        data=pending.data,
    )


# ── The fold rule, without a store ─────────────────────────────


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        ([], None),
        ([_user()], None),
        ([_compaction()], None),
        ([_info_error()], None),
        ([_text("API Error: late", is_meta=True)], None),
        ([_pending("queued")], None),
        ([_error()], "error"),
        ([_disconnect()], "disconnected"),
        ([_text("Working.")], ""),
        ([_text("  \n API Error: Overloaded")], "error"),
        ([_text("API Error: Overloaded"), _tool_output()], ""),
        ([_error(), _text("Back online.")], ""),
        ([_text("Working."), _error()], "error"),
        ([_text("Working."), _disconnect()], "disconnected"),
        ([_error(), _disconnect()], "disconnected"),
        ([_disconnect(), _text("Back online.")], ""),
        ([_error(), _info_error()], "error"),
        ([_text("Working."), _info_error()], ""),
    ],
)
def test_fold_rule(batch: list[NewConversationItem], expected: str | None) -> None:
    assert fold_activity_error(batch) == expected


def test_pending_input_never_decides() -> None:
    pending_error = NewConversationItem(
        type="error",
        response_id=PENDING_INPUT_RESPONSE_ID,
        data=ErrorData(source="execution", code="rate_limit_exceeded", message="x"),
    )
    assert fold_activity_error([_text("ok"), pending_error]) == ""


# ── Maintenance on append ──────────────────────────────────────


def _label(store: SqlAlchemyConversationStore, conversation_id: str) -> str | None:
    conv = store.get_conversation(conversation_id)
    assert conv is not None
    return conv.labels.get(_KEY)


@contextmanager
def _count_label_writes(store: SqlAlchemyConversationStore) -> Iterator[list[str]]:
    """Collect the non-SELECT statements that touch the labels table."""
    statements: list[str] = []
    table = SqlConversationLabel.__table__.name

    def on_exec(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if table in statement and not statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(store._conv_engine, "before_cursor_execute", on_exec)
    try:
        yield statements
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", on_exec)


def test_error_then_assistant_reply_clears(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_user(), _error()])
    assert _label(conversation_store, conv.id) == "error"

    conversation_store.append(conv.id, [_text("Back online.", response_id="r2")])
    assert _label(conversation_store, conv.id) == ""


def test_assistant_then_error_is_error(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_user(), _text("Working.")])
    assert _label(conversation_store, conv.id) == ""

    conversation_store.append(conv.id, [_error(response_id="r2")])
    assert _label(conversation_store, conv.id) == "error"


def test_info_error_is_ignored(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_text("Working.")])
    conversation_store.append(conv.id, [_info_error(response_id="r2")])
    assert _label(conversation_store, conv.id) == ""

    conversation_store.append(conv.id, [_error(response_id="r3")])
    conversation_store.append(conv.id, [_info_error(response_id="r4")])
    assert _label(conversation_store, conv.id) == "error"


def test_disconnect_after_error_then_recovery(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_error()])
    conversation_store.append(conv.id, [_disconnect(response_id="r2")])
    assert _label(conversation_store, conv.id) == "disconnected"

    conversation_store.append(conv.id, [_text("Back online.", response_id="r3")])
    assert _label(conversation_store, conv.id) == ""


def test_api_error_text_flags_until_the_next_reply(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_text("API Error: Overloaded")])
    assert _label(conversation_store, conv.id) == "error"

    conversation_store.append(conv.id, [_text("Retried fine.", response_id="r2")])
    assert _label(conversation_store, conv.id) == ""


def test_user_input_does_not_clear_an_error(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Only assistant output clears an error; a new user message waits for the reply."""
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_error()])
    conversation_store.append(conv.id, [_user("try again", response_id="r2")])
    assert _label(conversation_store, conv.id) == "error"


def test_unchanged_value_writes_nothing(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_error()])

    with _count_label_writes(conversation_store) as writes:
        conversation_store.append(conv.id, [_error(response_id="r2")])
        assert writes == []
        conversation_store.append(conv.id, [_user("again", response_id="r2")])
        assert writes == []
        conversation_store.append(conv.id, [_text("Back.", response_id="r3")])
        assert len(writes) == 1
    assert _label(conversation_store, conv.id) == ""


def test_pending_replacement_does_not_change_the_label(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A delivered user input is ignored, so replacing its slot cannot move the label.

    The receipt reuses the queued slot's position, which can sit below newer
    history; the fold never reads history, so that position does not matter.
    """
    conv = conversation_store.create_conversation()
    pending = conversation_store.append(conv.id, [_pending("queued")])[0]
    conversation_store.append(conv.id, [_error()])
    assert _label(conversation_store, conv.id) == "error"

    with _count_label_writes(conversation_store) as writes:
        conversation_store.append(conv.id, [_delivered(pending)])
        assert writes == []
    assert _label(conversation_store, conv.id) == "error"


def test_duplicate_repost_leaves_the_label(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    error = _error().model_copy(update={"stable_id": "a" * 32})
    conversation_store.append(conv.id, [error])
    conversation_store.append(conv.id, [_text("fine")])
    conversation_store.append(conv.id, [error])
    assert _label(conversation_store, conv.id) == ""


def test_concurrent_appends_to_one_session_keep_a_consistent_label(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Appends serialize on the conversation lock; the label ends on the newest decisive item."""
    conv = conversation_store.create_conversation()
    conversation_store.append(conv.id, [_user("hi")])
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(10):
                item = _error(response_id=f"w{n}_{i}") if (n + i) % 2 else _text("ok")
                conversation_store.append(conv.id, [item])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    history = conversation_store.list_items(conv.id, limit=100).data
    assert len(history) == 61
    assert _label(conversation_store, conv.id) == (fold_activity_error(history) or "")
