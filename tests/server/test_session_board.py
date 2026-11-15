"""Unit tests for the Board's server-owned acceptance stamp."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.session_board import (
    BoardAcceptance,
    drop_board_acceptance_seed,
    resolve_board_acceptance,
    write_board_acceptance,
)


class _Store:
    def __init__(self, updated_at: int | None, accepts: bool = True) -> None:
        self.updated_at = updated_at
        self.accepts = accepts
        self.accepted: list[tuple[str, int, str]] = []
        self.labels: list[dict[str, str]] = []

    def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
        if self.updated_at is None:
            return None
        return SimpleNamespace(id=conversation_id, updated_at=self.updated_at)

    def accept_board_review(self, conversation_id: str, reviewed: int, stamp: str) -> bool:
        self.accepted.append((conversation_id, reviewed, stamp))
        return self.accepts

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.append(updates)


async def _resolve(updated_at: int | None, requested: str, now: float) -> BoardAcceptance:
    return await resolve_board_acceptance(
        _Store(updated_at),  # type: ignore[arg-type]
        "conv_a",
        requested,
        clock=lambda: now,
    )


@pytest.mark.parametrize("now", [1_700_000_102.0, 1_700_003_600.5])
async def test_stamp_is_one_second_past_the_reviewed_activity(now: float) -> None:
    """The stamp doesn't depend on when Accept ran, only on the reviewed second."""
    assert await _resolve(1_700_000_100, "1700000100", now) == BoardAcceptance(
        reviewed=1_700_000_100, stamp="1700000101"
    )


async def test_reopen_needs_no_review() -> None:
    assert await _resolve(None, "", 1_700_000_200.0) == BoardAcceptance(reviewed=None, stamp="")


@pytest.mark.parametrize(
    ("updated_at", "requested", "now", "code"),
    [
        # Changed since the user reviewed it.
        (1_700_000_120, "1700000100", 1_700_000_200.0, ErrorCode.CONFLICT),
        # Too recent: a write sampled in the reviewed second may still commit.
        (1_700_000_100, "1700000100", 1_700_000_100.9, ErrorCode.CONFLICT),
        (1_700_000_100, "1700000100", 1_700_000_101.9, ErrorCode.CONFLICT),
        (1_700_000_100, "soon", 1_700_000_200.0, ErrorCode.INVALID_INPUT),
        (None, "1700000100", 1_700_000_200.0, ErrorCode.NOT_FOUND),
    ],
)
async def test_refuses_stale_recent_malformed_or_missing_acceptance(
    updated_at: int | None, requested: str, now: float, code: str
) -> None:
    with pytest.raises(OmnigentError) as raised:
        await _resolve(updated_at, requested, now)
    assert raised.value.code == code


async def test_write_goes_through_the_stores_review_check() -> None:
    store = _Store(1_700_000_100)
    await write_board_acceptance(store, "conv_a", BoardAcceptance(1_700_000_100, "1700000101"))  # type: ignore[arg-type]
    assert store.accepted == [("conv_a", 1_700_000_100, "1700000101")]


async def test_write_refuses_when_the_session_changed_after_validation() -> None:
    store = _Store(1_700_000_100, accepts=False)
    with pytest.raises(OmnigentError) as raised:
        await write_board_acceptance(store, "conv_a", BoardAcceptance(1_700_000_100, "1700000101"))  # type: ignore[arg-type]
    assert raised.value.code == ErrorCode.CONFLICT


async def test_reopen_clears_the_label() -> None:
    store = _Store(1_700_000_100)
    await write_board_acceptance(store, "conv_a", BoardAcceptance(None, ""))  # type: ignore[arg-type]
    assert store.labels == [{"omnigent.board.accepted": ""}]
    assert store.accepted == []


def test_new_session_labels_drop_an_inherited_acceptance() -> None:
    drop_board_acceptance_seed(None)
    for value in ("9999999999", ""):
        labels = {"omnigent.board.accepted": value, "kept": "yes"}
        drop_board_acceptance_seed(labels)
        assert labels == {"kept": "yes"}
