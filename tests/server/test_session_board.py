"""Unit tests for the Board's server-owned acceptance."""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.session_board import (
    BoardAcceptance,
    drop_board_acceptance_seed,
    resolve_board_acceptance,
    write_board_acceptance,
)
from omnigent.stores.conversation_store import board_revision


def _conversation(**overrides: object) -> Conversation:
    fields: dict[str, object] = {
        "id": "conv_a",
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_100,
        "root_conversation_id": "conv_a",
        "item_revision": 4,
    }
    fields.update(overrides)
    return Conversation(**fields)  # type: ignore[arg-type]


class _Store:
    def __init__(self, conversation: Conversation | None, stamp: str | None = "1") -> None:
        self.conversation = conversation
        self.stamp = stamp
        self.accepted: list[tuple[str, str]] = []
        self.labels: list[dict[str, str]] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.conversation

    def accept_board_review(self, conversation_id: str, reviewed: str) -> str | None:
        self.accepted.append((conversation_id, reviewed))
        return self.stamp

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.append(updates)


async def _resolve(conversation: Conversation | None, requested: str) -> BoardAcceptance:
    return await resolve_board_acceptance(_Store(conversation), "conv_a", requested)  # type: ignore[arg-type]


def test_revision_tells_apart_writes_that_share_a_second() -> None:
    reviewed = board_revision(_conversation())
    assert board_revision(_conversation(labels={"any": "x"})) == reviewed
    for changed in (
        {"item_revision": 5},
        {"updated_at": 1_700_000_101},
        {"title": "Renamed"},
        {"archived": True},
        {"model_override": "other"},
    ):
        assert board_revision(_conversation(**changed)) != reviewed, changed


async def test_resolves_the_current_revision_and_reopen() -> None:
    current = _conversation()
    assert await _resolve(current, board_revision(current)) == BoardAcceptance(
        board_revision(current)
    )
    assert await _resolve(None, "") == BoardAcceptance(None)


@pytest.mark.parametrize(
    ("conversation", "requested", "code"),
    [
        (_conversation(item_revision=5), board_revision(_conversation()), ErrorCode.CONFLICT),
        (None, "anything", ErrorCode.NOT_FOUND),
    ],
)
async def test_refuses_a_stale_review_or_missing_session(
    conversation: Conversation | None, requested: str, code: str
) -> None:
    with pytest.raises(OmnigentError) as raised:
        await _resolve(conversation, requested)
    assert raised.value.code == code


async def test_write_goes_through_the_stores_review_check() -> None:
    store = _Store(_conversation())
    await write_board_acceptance(store, "conv_a", BoardAcceptance("rev"))  # type: ignore[arg-type]
    assert store.accepted == [("conv_a", "rev")]


async def test_write_refuses_when_the_session_changed_after_validation() -> None:
    store = _Store(_conversation(), stamp=None)
    with pytest.raises(OmnigentError) as raised:
        await write_board_acceptance(store, "conv_a", BoardAcceptance("rev"))  # type: ignore[arg-type]
    assert raised.value.code == ErrorCode.CONFLICT


async def test_reopen_clears_the_label() -> None:
    store = _Store(_conversation())
    await write_board_acceptance(store, "conv_a", BoardAcceptance(None))  # type: ignore[arg-type]
    assert store.labels == [{"omnigent.board.accepted": ""}]
    assert store.accepted == []


def test_new_session_labels_drop_an_inherited_acceptance() -> None:
    drop_board_acceptance_seed(None)
    for value in ("9999999999", ""):
        labels = {"omnigent.board.accepted": value, "kept": "yes"}
        drop_board_acceptance_seed(labels)
        assert labels == {"kept": "yes"}
