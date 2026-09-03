"""Pin the single session fetch on the REPL's startup attach.

``_attach_to_conversation`` reads the session once to fail loud on a bad
id, then hands the adapter the same snapshot. Before that handoff the
adapter re-read ``GET /v1/sessions/{id}`` immediately afterwards, so every
``omnigent run`` — fresh or resumed — paid two round trips for one
unchanged document. On a hosted server that is ~150-250ms of dead time in
the last stretch before the REPL paints.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._repl import _SessionsChatReplAdapter

pytestmark = pytest.mark.asyncio


@dataclass
class _StubSession:
    """Minimal Session-shaped dataclass for snapshot returns."""

    id: str
    agent_id: str = "ag_x"
    runner_id: str | None = None
    reasoning_effort: str | None = None
    model_override: str | None = None
    agent_name: str | None = None
    llm_model: str | None = None
    context_window: int | None = None
    last_total_tokens: int | None = None
    harness: str | None = None


def _build_adapter(session_id: str, snapshot: _StubSession) -> _SessionsChatReplAdapter:
    """
    Build an attach-mode adapter whose ``sessions.get`` is observable.

    :param session_id: Existing session the adapter attaches to, e.g.
        ``"conv_abc123"``.
    :param snapshot: Session the mocked ``sessions.get`` returns.
    :returns: An adapter with bind/stream/recovery stubbed out so
        ``_ensure_session`` runs without HTTP or asyncio plumbing.
    """
    client = MagicMock()
    client.sessions.get = AsyncMock(return_value=snapshot)
    adapter = _SessionsChatReplAdapter(client=client, agent_name="t", session_id=session_id)
    adapter._bind_runner_if_needed = AsyncMock(return_value=None)  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock(return_value=None)  # type: ignore[method-assign]
    adapter._stream_pump = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return adapter


async def test_matching_snapshot_skips_the_refetch() -> None:
    """A handed-in snapshot for this session replaces the GET."""
    snap = _StubSession(id="conv_a", runner_id="runner_1", harness="claude-sdk")
    adapter = _build_adapter("conv_a", snap)

    assert await adapter._ensure_session(snapshot=snap) == "conv_a"

    adapter._client.sessions.get.assert_not_awaited()  # type: ignore[attr-defined]
    # The snapshot still hydrates the adapter — skipping the fetch must
    # not skip the state it carried.
    assert adapter._bound_runner_id == "runner_1"
    assert adapter._harness == "claude-sdk"


async def test_snapshot_for_another_session_is_ignored() -> None:
    """A mismatched snapshot must not be trusted for this session."""
    other = _StubSession(id="conv_other", runner_id="runner_other")
    mine = _StubSession(id="conv_a", runner_id="runner_mine")
    adapter = _build_adapter("conv_a", mine)

    await adapter._ensure_session(snapshot=other)

    adapter._client.sessions.get.assert_awaited_once_with("conv_a")  # type: ignore[attr-defined]
    assert adapter._bound_runner_id == "runner_mine"


async def test_no_snapshot_still_fetches() -> None:
    """Callers that hold no snapshot keep the original fetch."""
    mine = _StubSession(id="conv_a", runner_id="runner_mine")
    adapter = _build_adapter("conv_a", mine)

    await adapter._ensure_session()

    adapter._client.sessions.get.assert_awaited_once_with("conv_a")  # type: ignore[attr-defined]


async def test_attach_reads_the_session_exactly_once() -> None:
    """The startup attach makes one ``GET /v1/sessions/{id}``, not two."""
    from omnigent.repl import _repl

    snap = _StubSession(id="conv_a", runner_id="runner_1")
    adapter = _build_adapter("conv_a", snap)
    # Empty conversation: attach returns right after the items fetch, so
    # the rendering path needs no stubbing.
    adapter._client.sessions.list_items = AsyncMock(  # type: ignore[attr-defined]
        return_value=MagicMock(data=[], has_more=False)
    )

    await _repl._attach_to_conversation(
        "conv_a",
        adapter,
        adapter._client,
        MagicMock(),
        MagicMock(),
        ui_name="t",
        redraw_screen=False,
    )

    assert adapter._client.sessions.get.await_count == 1  # type: ignore[attr-defined]
