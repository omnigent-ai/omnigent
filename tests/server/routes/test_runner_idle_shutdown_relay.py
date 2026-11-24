"""Relay-bound sessions of an idle-shut-down runner settle idle instead of failing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest

from omnigent.entities import Conversation
from omnigent.runtime import session_stream
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration

_TASK_TIMEOUT_S = 10.0
_RUNNER = "runner-idle-shutdown"


def _event_attributes(
    caplog: pytest.LogCaptureFixture, event_name: str
) -> list[dict[str, object]]:
    return [
        cast("dict[str, object]", getattr(record, "attributes", {}))
        for record in caplog.records
        if getattr(record, "event_name", None) == event_name
    ]


def test_idle_shutdown_marks_this_runners_unclaimed_sessions_and_logs_mismatches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Relay-bound and stored sessions are marked; existing intent and other runners are not."""
    caplog.set_level(logging.INFO)
    relays = {
        "relay-running": SimpleNamespace(runner_id=_RUNNER),
        "relay-idle": SimpleNamespace(runner_id=_RUNNER),
        "relay-stopping": SimpleNamespace(runner_id=_RUNNER),
        "relay-other": SimpleNamespace(runner_id="runner-other"),
    }
    stored = [
        Conversation(
            id="stored-waiting",
            root_conversation_id="stored-waiting",
            created_at=0,
            updated_at=0,
            runner_id=_RUNNER,
        ),
        Conversation(
            id="stored-other",
            root_conversation_id="stored-other",
            created_at=0,
            updated_at=0,
            runner_id="runner-other",
        ),
    ]
    statuses = {
        "relay-running": "running",
        "relay-idle": "idle",
        "relay-stopping": "running",
        "stored-waiting": "waiting",
    }
    session_ids = [*relays, "stored-waiting", "stored-other"]
    try:
        for session_id, handle in relays.items():
            sessions._runner_relay_tasks[session_id] = handle  # type: ignore[assignment]
        for session_id, status in statuses.items():
            sessions._session_status_cache[session_id] = status
        sessions._intentional_stop_sessions["relay-stopping"] = "runner-earlier"

        marked = orchestration._note_runner_idle_shutdown(_RUNNER, stored)

        assert marked == 3
        assert {
            session_id
            for session_id in session_ids
            if sessions._intentional_stop_sessions.get(session_id) == _RUNNER
        } == {"relay-running", "relay-idle", "stored-waiting"}
        assert sessions._intentional_stop_sessions["relay-stopping"] == "runner-earlier"
        mismatches = _event_attributes(caplog, "runner_idle_shutdown_status_mismatch")
        assert sorted(cast("str", event["cached_session_status"]) for event in mismatches) == [
            "running",
            "waiting",
        ]
    finally:
        for session_id in session_ids:
            sessions._runner_relay_tasks.pop(session_id, None)
            sessions._session_status_cache.pop(session_id, None)
            sessions._intentional_stop_sessions.pop(session_id, None)


class _DropStreamResponse:
    """Runner stream that sends its ready heartbeat, then loses the tunnel."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def __aenter__(self) -> _DropStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The stream represents a successful HTTP response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._gate.wait()
        raise ConnectionError("tunnel closed before request completed")


class _DropRunnerClient:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def stream(self, method: str, path: str, *, timeout: Any) -> _DropStreamResponse:
        del method, path, timeout
        return _DropStreamResponse(self._gate)


class _LabelStore:
    def __init__(self) -> None:
        self.labels: dict[str, dict[str, str]] = {}

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.setdefault(conversation_id, {}).update(updates)

    def get_runner_liveness(self, conversation_id: str) -> None:
        return None

    def get_conversation(self, conversation_id: str) -> None:
        return None


@pytest.mark.parametrize("idle_shutdown", [True, False])
async def test_relay_settles_an_idle_shut_down_runners_session_instead_of_failing_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    idle_shutdown: bool,
) -> None:
    """A session still cached as running settles idle after its runner's idle shutdown."""
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.0)
    caplog.set_level(logging.INFO)
    sessions._runner_relay_tasks.clear()
    session_id = "0123456789abcdef0123456789abcdef"
    gate = asyncio.Event()
    try:
        handle = await sessions._ensure_runner_relay_ready(
            session_id,
            _RUNNER,
            _DropRunnerClient(gate),  # type: ignore[arg-type]
            conversation_store=_LabelStore(),  # type: ignore[arg-type]
        )
        assert handle is not None
        sessions._session_status_cache[session_id] = "running"
        if idle_shutdown:
            orchestration._note_runner_idle_shutdown(_RUNNER)

        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        assert handle.task.exception() is None
        expected_status = "idle" if idle_shutdown else "failed"
        assert sessions._session_status_cache.get(session_id) == expected_status
        [outcome] = _event_attributes(caplog, "runner_stream_disconnected")
        assert outcome["decision"] == ("intentional_stop" if idle_shutdown else "failed_mid_turn")
    finally:
        gate.set()
        handle = sessions._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions._runner_relay_tasks.clear()
        sessions._session_status_cache.pop(session_id, None)
        sessions._intentional_stop_sessions.pop(session_id, None)
        session_stream.close(session_id)
