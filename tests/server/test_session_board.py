"""Unit tests for the Board's server-owned acceptance stamp."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.session_board import reject_board_acceptance_seed, stamp_board_acceptance


class _Store:
    def __init__(self, updated_at: int | None) -> None:
        self.updated_at = updated_at

    def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
        if self.updated_at is None:
            return None
        return SimpleNamespace(id=conversation_id, updated_at=self.updated_at)


async def _stamp(updated_at: int | None, requested: str, now: float) -> str:
    return await stamp_board_acceptance(
        _Store(updated_at),  # type: ignore[arg-type]
        "conv_a",
        requested,
        clock=lambda: now,
    )


@pytest.mark.parametrize("now", [1_700_000_102.0, 1_700_003_600.5])
async def test_stamp_is_one_second_past_the_reviewed_activity(now: float) -> None:
    """The stamp doesn't depend on when Accept ran, only on the reviewed second."""
    assert await _stamp(1_700_000_100, "1700000100", now) == "1700000101"


async def test_reopen_is_stored_as_an_empty_value() -> None:
    assert await _stamp(None, "", 1_700_000_200.0) == ""


@pytest.mark.parametrize(
    ("updated_at", "requested", "now", "code"),
    [
        # Changed since the user reviewed it.
        (1_700_000_120, "1700000100", 1_700_000_200.0, ErrorCode.CONFLICT),
        # Still the reviewed second: a later write in it would look reviewed.
        (1_700_000_100, "1700000100", 1_700_000_100.9, ErrorCode.CONFLICT),
        # A writer that read the clock in the reviewed second may not have committed.
        (1_700_000_100, "1700000100", 1_700_000_101.9, ErrorCode.CONFLICT),
        (1_700_000_100, "soon", 1_700_000_200.0, ErrorCode.INVALID_INPUT),
        (None, "1700000100", 1_700_000_200.0, ErrorCode.NOT_FOUND),
    ],
)
async def test_refuses_stale_same_second_malformed_or_missing_acceptance(
    updated_at: int | None, requested: str, now: float, code: str
) -> None:
    with pytest.raises(OmnigentError) as raised:
        await _stamp(updated_at, requested, now)
    assert raised.value.code == code


def test_new_sessions_cannot_seed_an_acceptance() -> None:
    reject_board_acceptance_seed(None)
    reject_board_acceptance_seed({"other": "1"})
    with pytest.raises(OmnigentError) as raised:
        reject_board_acceptance_seed({"omnigent.board.accepted": "9999999999"})
    assert raised.value.code == ErrorCode.INVALID_INPUT
