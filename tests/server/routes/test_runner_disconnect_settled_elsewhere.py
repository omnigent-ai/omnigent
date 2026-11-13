"""A disconnect decision yields to a row another replica settled over a stale local cache."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.entities import Conversation
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import orchestration

SESSION_ID = "9e1d0f4e5f6c4a5b8c7d6e5f4a3b2c1d"
RUNNER_ID = "runner_token_settled_elsewhere"
_LOGGER = "omnigent.server.routes._sessions.orchestration"


class _Store:
    """Conversation store that serves one row, or raises, and counts reads."""

    def __init__(self, conv: Conversation | None, error: Exception | None = None) -> None:
        self.conv = conv
        self.error = error
        self.reads = 0

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        self.reads += 1
        if self.error is not None:
            raise self.error
        if self.conv is not None and self.conv.id == conversation_id:
            return self.conv
        return None


def _row(live_status: str | None) -> Conversation:
    return Conversation(
        id=SESSION_ID,
        created_at=1,
        updated_at=1,
        root_conversation_id=SESSION_ID,
        runner_id=RUNNER_ID,
        live_status=live_status,
    )


def _decision(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    record = next(
        r for r in caplog.records if getattr(r, "event_name", None) == "runner_disconnect_decision"
    )
    attributes: dict[str, Any] = record.__dict__["attributes"]
    return attributes


@pytest.fixture(autouse=True)
def _isolated_session_state() -> Iterator[None]:
    orchestration._session_status_cache.pop(SESSION_ID, None)
    orchestration._session_active_response_cache.pop(SESSION_ID, None)
    session_live_state._last_status_at.pop(SESSION_ID, None)
    yield
    orchestration._session_status_cache.pop(SESSION_ID, None)
    orchestration._session_active_response_cache.pop(SESSION_ID, None)
    session_live_state._last_status_at.pop(SESSION_ID, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["runner_offline_sweep", "runner_disconnected_mid_turn"])
@pytest.mark.parametrize("cached", ["running", "waiting"])
@pytest.mark.parametrize("settled", ["idle", "failed"])
async def test_row_settled_by_another_replica_overrides_stale_mid_turn_cache(
    caplog: pytest.LogCaptureFixture, origin: str, cached: str, settled: str
) -> None:
    """A turn that ended through another replica is not failed by this one's stale cache."""
    orchestration._session_status_cache[SESSION_ID] = cached
    orchestration._session_active_response_cache[SESSION_ID] = "resp_stale"
    session_live_state._last_status_at[SESSION_ID] = (
        time.monotonic() - orchestration._LIVE_STATUS_WRITE_SETTLE_S - 1.0
    )
    store = _Store(_row(settled))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        failed = await orchestration._runner_disconnect_requires_failure(
            SESSION_ID,
            store,  # type: ignore[arg-type]
            origin=origin,
            snapshot=_row(cached) if origin == "runner_offline_sweep" else None,
        )

    assert failed is False
    assert store.reads == 1
    attributes = _decision(caplog)
    assert attributes["decision"] == "settled_on_another_replica"
    assert attributes["status_source"] == "cache"
    assert attributes["cached_session_status"] == cached
    assert attributes["persisted_session_status"] == settled
    assert attributes["live_status_write_age_s"] >= orchestration._LIVE_STATUS_WRITE_SETTLE_S
    # The stale edge is dropped so reads fall back to the row.
    assert SESSION_ID not in orchestration._session_status_cache
    assert SESSION_ID not in orchestration._session_active_response_cache


@pytest.mark.asyncio
async def test_unrecorded_local_edge_still_fails_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cached edge this replica never wrote cannot be ordered against the row."""
    orchestration._session_status_cache[SESSION_ID] = "running"
    store = _Store(_row("idle"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        failed = await orchestration._runner_disconnect_requires_failure(
            SESSION_ID,
            store,  # type: ignore[arg-type]
            origin="runner_offline_sweep",
            snapshot=_row("running"),
        )

    assert failed is True
    attributes = _decision(caplog)
    assert attributes["decision"] == "failed_mid_turn"
    assert attributes["live_status_write_age_s"] is None
    assert orchestration._session_status_cache[SESSION_ID] == "running"


@pytest.mark.asyncio
async def test_fresh_local_edge_still_fails_the_turn(caplog: pytest.LogCaptureFixture) -> None:
    """A row read right after this replica's own running edge may predate that edge."""
    orchestration._session_status_cache[SESSION_ID] = "running"
    session_live_state._last_status_at[SESSION_ID] = time.monotonic()
    store = _Store(_row("idle"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        failed = await orchestration._runner_disconnect_requires_failure(
            SESSION_ID,
            store,  # type: ignore[arg-type]
            origin="runner_offline_sweep",
            snapshot=_row("running"),
        )

    assert failed is True
    attributes = _decision(caplog)
    assert attributes["decision"] == "failed_mid_turn"
    assert 0.0 <= attributes["live_status_write_age_s"] < 5.0
    assert orchestration._session_status_cache[SESSION_ID] == "running"


@pytest.mark.asyncio
async def test_aged_local_edge_yields_to_the_settled_row(caplog: pytest.LogCaptureFixture) -> None:
    """Once this replica's write has had time to land, a settled row is the newer edge."""
    orchestration._session_status_cache[SESSION_ID] = "running"
    session_live_state._last_status_at[SESSION_ID] = (
        time.monotonic() - orchestration._LIVE_STATUS_WRITE_SETTLE_S - 1.0
    )
    store = _Store(_row("idle"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        failed = await orchestration._runner_disconnect_requires_failure(
            SESSION_ID,
            store,  # type: ignore[arg-type]
            origin="runner_offline_sweep",
            snapshot=_row("running"),
        )

    assert failed is False
    assert _decision(caplog)["decision"] == "settled_on_another_replica"


@pytest.mark.asyncio
@pytest.mark.parametrize("row_status", ["running", "waiting", None])
async def test_mid_turn_or_statusless_row_keeps_the_failure(
    caplog: pytest.LogCaptureFixture, row_status: str | None
) -> None:
    """Only a row that another replica settled can override the cache."""
    orchestration._session_status_cache[SESSION_ID] = "running"
    store = _Store(_row(row_status))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        failed = await orchestration._runner_disconnect_requires_failure(
            SESSION_ID,
            store,  # type: ignore[arg-type]
            origin="runner_offline_sweep",
            snapshot=_row("running"),
        )

    assert failed is True
    assert _decision(caplog)["decision"] == "failed_mid_turn"
    assert orchestration._session_status_cache[SESSION_ID] == "running"


@pytest.mark.asyncio
async def test_unreadable_row_keeps_the_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A failed read proves nothing about the row, so the cached turn still fails."""
    orchestration._session_status_cache[SESSION_ID] = "running"
    store = _Store(None, error=RuntimeError("metadata unavailable"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        failed = await orchestration._runner_disconnect_requires_failure(
            SESSION_ID,
            store,  # type: ignore[arg-type]
            origin="runner_offline_sweep",
            snapshot=_row("running"),
        )

    assert failed is True
    attributes = _decision(caplog)
    assert attributes["decision"] == "failed_mid_turn"
    assert attributes["status_lookup"] == "error"


@pytest.mark.asyncio
async def test_idle_cache_still_skips_the_row_read() -> None:
    """A cached idle is already a no-failure answer; no extra read is spent on it."""
    orchestration._session_status_cache[SESSION_ID] = "idle"
    store = _Store(_row("running"))

    failed = await orchestration._runner_disconnect_requires_failure(
        SESSION_ID,
        store,  # type: ignore[arg-type]
        origin="runner_offline_sweep",
        snapshot=_row("idle"),
    )

    assert failed is False
    assert store.reads == 0


def test_live_status_write_age_tracks_enqueued_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The age starts at the enqueued write and is forgotten with the session's state."""
    assert session_live_state.live_status_write_age_s(SESSION_ID) is None
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        session_live_state,
        "_store",
        type(
            "Store",
            (),
            {"set_session_live_status": lambda self, sid, st: writes.append((sid, st))},
        )(),
    )
    monkeypatch.setattr(session_live_state, "submit", lambda name, fn, *a, **kw: fn(*a))
    session_live_state._last_status.pop(SESSION_ID, None)

    session_live_state.persist_live_status(SESSION_ID, "running")
    age = session_live_state.live_status_write_age_s(SESSION_ID)
    assert age is not None and 0.0 <= age < 1.0
    assert writes == [(SESSION_ID, "running")]

    session_live_state.forget_live_status(SESSION_ID)
    assert session_live_state.live_status_write_age_s(SESSION_ID) is None
    session_live_state._last_status.pop(SESSION_ID, None)
