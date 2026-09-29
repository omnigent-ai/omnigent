"""Diagnostics naming the source behind runner-drop and offline-sweep verdicts."""

from __future__ import annotations

import logging
import time

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.common import _session_status_cache
from omnigent.server.schemas import ErrorDetail

_SID = "conv_drop_src"
_DROP = "runner_drop_status_source"
_SWEEP = "runner_offline_sweep_session"


def _conv(live_status: str | None, **kw: object) -> Conversation:
    return Conversation(
        id=_SID,
        created_at=1,
        updated_at=1,
        root_conversation_id=_SID,
        runner_id="runner_1",
        live_status=live_status,
        **kw,  # type: ignore[arg-type]
    )


class _Store:
    def __init__(self, conv: Conversation | None = None, exc: Exception | None = None) -> None:
        self._conv = conv
        self._exc = exc

    def get_conversation(self, session_id: str) -> Conversation | None:
        if self._exc is not None:
            raise self._exc
        return self._conv


@pytest.fixture(autouse=True)
def _clean_cache():
    _session_status_cache.pop(_SID, None)
    yield
    _session_status_cache.pop(_SID, None)


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == name]


@pytest.mark.parametrize(
    ("row", "expected"), [("running", True), ("waiting", True), ("idle", False)]
)
async def test_cold_cache_row(caplog, monkeypatch, row, expected) -> None:
    caplog.set_level(logging.INFO)
    now = 1_000_000
    monkeypatch.setattr(orchestration.time, "time", lambda: float(now))
    conv = _conv(row, runner_last_seen=now - 30, parent_conversation_id="conv_p")
    conv.pending_elicitation_count = 2
    assert await orchestration._runner_drop_interrupted_turn(_SID, _Store(conv)) is expected
    (rec,) = _events(caplog, _DROP)
    attrs = rec.attributes
    assert rec.session_id == _SID
    assert attrs["status_source"] == "row"
    assert attrs["row_live_status"] == row
    assert attrs["interrupted"] is expected
    assert attrs["runner_id"] == "runner_1"
    assert attrs["runner_last_seen_age_s"] == 30
    assert attrs["pending_elicitation_count"] == 2
    assert attrs["is_sub_agent"] is True
    assert "harness_override" in attrs


async def test_missing_row(caplog) -> None:
    caplog.set_level(logging.INFO)
    assert await orchestration._runner_drop_interrupted_turn(_SID, _Store(None)) is True
    (rec,) = _events(caplog, _DROP)
    assert rec.attributes["status_source"] == "row_missing"
    assert rec.attributes["row_live_status"] is None
    assert rec.attributes["interrupted"] is True


async def test_unreadable_row(caplog) -> None:
    caplog.set_level(logging.INFO)
    store = _Store(exc=RuntimeError("db down"))
    assert await orchestration._runner_drop_interrupted_turn(_SID, store) is True
    (rec,) = _events(caplog, _DROP)
    assert rec.attributes["status_source"] == "row_unreadable"
    assert rec.attributes["interrupted"] is True


async def test_warm_cache_is_silent(caplog) -> None:
    caplog.set_level(logging.INFO)
    _session_status_cache[_SID] = "running"
    assert await orchestration._runner_drop_interrupted_turn(_SID, _Store(_conv("idle"))) is True
    assert _events(caplog, _DROP) == []


@pytest.fixture()
def sweep_calls(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _publish(session_id, status, error, **kw):
        calls.append((session_id, status))

    async def _labels(*a, **k):
        return None

    monkeypatch.setattr(orchestration, "_publish_status", _publish)
    monkeypatch.setattr(orchestration, "_persist_session_status_error_labels", _labels)
    return calls


_ERR = ErrorDetail(code="runner_disconnected", message="gone")


async def test_sweep_cold_cache_row_running(caplog, sweep_calls) -> None:
    caplog.set_level(logging.INFO)
    await orchestration._mark_runner_sessions_offline_impl([_conv("running")], _ERR, _Store())
    assert sweep_calls == [(_SID, "failed")]
    (rec,) = _events(caplog, _SWEEP)
    assert rec.attributes["status_source"] == "row"
    assert rec.attributes["row_live_status"] == "running"
    assert rec.attributes["cached_session_status"] is None
    assert rec.attributes["runner_id"] == "runner_1"


async def test_sweep_warm_cache(caplog, sweep_calls) -> None:
    caplog.set_level(logging.INFO)
    _session_status_cache[_SID] = "running"
    await orchestration._mark_runner_sessions_offline_impl([_conv("idle")], _ERR, _Store())
    assert sweep_calls == [(_SID, "failed")]
    (rec,) = _events(caplog, _SWEEP)
    assert rec.attributes["status_source"] == "cache"
    assert rec.attributes["cached_session_status"] == "running"
    assert rec.attributes["row_live_status"] == "idle"


async def test_sweep_idle_session_not_logged(caplog, sweep_calls) -> None:
    caplog.set_level(logging.INFO)
    await orchestration._mark_runner_sessions_offline_impl([_conv("idle")], _ERR, _Store())
    assert sweep_calls == []
    assert _events(caplog, _SWEEP) == []


def test_persist_failure_is_structured(caplog) -> None:
    class _Failing:
        def set_session_live_status(self, session_id: str, status: str) -> None:
            raise ValueError("boom")

    caplog.set_level(logging.WARNING)
    session_live_state.configure(_Failing())  # type: ignore[arg-type]
    try:
        session_live_state.persist_live_status("conv_pf", "running")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _events(caplog, "live_status_persist_failed"):
            time.sleep(0.01)
    finally:
        session_live_state.configure(None)
    (rec,) = _events(caplog, "live_status_persist_failed")
    assert rec.session_id == "conv_pf"
    assert rec.attributes["live_status"] == "running"
    assert rec.attributes["error_type"] == "ValueError"
    assert rec.exc_info


@pytest.mark.parametrize("builder_raises", [False, True])
def test_failed_write_still_evicts_dedupe_entry(caplog, builder_raises) -> None:
    class _Failing:
        def set_session_live_status(self, session_id: str, status: str) -> None:
            raise ValueError("boom")

    caplog.set_level(logging.WARNING)
    session_live_state.configure(_Failing())  # type: ignore[arg-type]
    orig = session_live_state.debug_event
    if builder_raises:

        def _bad(*a, **k):
            raise RuntimeError("diag")

        session_live_state.debug_event = _bad  # type: ignore[assignment]
    try:
        session_live_state.persist_live_status("conv_ev", "running")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "conv_ev" in session_live_state._last_status:
            time.sleep(0.01)
        assert "conv_ev" not in session_live_state._last_status
        assert any("write failed" in r.getMessage() for r in caplog.records)
    finally:
        session_live_state.debug_event = orig  # type: ignore[assignment]
        session_live_state.configure(None)
