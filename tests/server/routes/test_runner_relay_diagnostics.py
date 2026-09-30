"""Diagnostics for malformed runner relay frames and task failures."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

import pytest

from omnigent.server.routes._sessions import orchestration


class _RawFrameResponse:
    """Async stream response that yields already-encoded SSE frames."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def __aenter__(self) -> _RawFrameResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        for frame in self._frames:
            yield frame


class _RawFrameClient:
    """Fake runner client for raw SSE frames."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    def stream(self, method: str, path: str, *, timeout: Any) -> _RawFrameResponse:
        del method, path, timeout
        return _RawFrameResponse(self._frames)


@pytest.mark.asyncio
async def test_relay_logs_and_skips_malformed_and_non_object_frames(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bad frames are bounded diagnostics, and a later heartbeat still readies the relay."""
    session_id = "relay-diagnostics-malformed"
    secret = "private-frame-content"
    frames = [
        *(f'data: {{"private":"{secret}-{index}"\n\n' for index in range(20)),
        "data: null\n\n",
        "data: null\n\n",
        'data: ["private-array"]\n\n',
        'data: "private-string"\n\n',
        "data: 42\n\n",
        "data: {}\n\n",
        'data: {"type":null}\n\n',
        'data: {"type":"session.status","status":null}\n\n',
        'data: {"type":"session.status","status":"unknown"}\n\n',
        'data: {"type":"session.status","status":"failed","error":{"code":[]}}\n\n',
        'data: {"type":"response.in_progress","response":null}\n\n',
        'data: {"type":"response.completed","response":null}\n\n',
        'data: {"type":"session.heartbeat"}\n\n',
        "data: [DONE]\n\n",
    ]
    orchestration._runner_relay_tasks.clear()
    try:
        with caplog.at_level(logging.WARNING, logger=orchestration._logger.name):
            handle = orchestration._ensure_runner_relay(
                session_id,
                "runner-diagnostics",
                _RawFrameClient(frames),  # type: ignore[arg-type]
            )
            assert handle is not None
            await asyncio.wait_for(handle.ready.wait(), timeout=1.0)
            await asyncio.wait_for(handle.task, timeout=1.0)

        malformed = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "runner_stream_malformed_json"
        ]
        assert len(malformed) == 1
        assert malformed[0].session_id == session_id
        assert malformed[0].attributes["payload_length"] == len(frames[0][6:-2])
        assert malformed[0].attributes["decoder_line"] == 1
        assert malformed[0].attributes["decoder_column"] > 0
        assert malformed[0].attributes["suppressed_after_first"] is True

        non_object = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "runner_stream_non_object_json"
        ]
        assert len(non_object) == 1
        assert non_object[0].attributes["json_type"] == "NoneType"
        assert (
            sum(
                getattr(record, "event_name", None) == "runner_stream_invalid_event_type"
                for record in caplog.records
            )
            == 1
        )
        assert (
            sum(
                getattr(record, "event_name", None) == "runner_stream_invalid_session_status"
                for record in caplog.records
            )
            == 1
        )
        assert (
            sum(
                getattr(record, "event_name", None) == "runner_stream_invalid_status_error"
                for record in caplog.records
            )
            == 1
        )
        assert (
            sum(
                getattr(record, "event_name", None) == "runner_stream_invalid_response"
                for record in caplog.records
            )
            == 1
        )
        logged = " ".join(
            f"{record.getMessage()} {getattr(record, 'attributes', {})}"
            for record in caplog.records
        )
        assert secret not in logged
        assert "private-array" not in logged
        assert "private-string" not in logged
    finally:
        handle = orchestration._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handle.task
        orchestration._runner_relay_tasks.clear()


@pytest.mark.asyncio
async def test_relay_logs_json_recursion_and_continues(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parser recursion is dropped without preventing a later readiness heartbeat."""
    original_loads = orchestration.json.loads
    first_call = True

    def _loads(payload: str) -> Any:
        nonlocal first_call
        if first_call:
            first_call = False
            raise RecursionError("private recursive payload")
        return original_loads(payload)

    monkeypatch.setattr(orchestration.json, "loads", _loads)
    session_id = "relay-diagnostics-recursion"
    frames = [
        'data: {"private":"content"}\n\n',
        'data: {"type":"session.heartbeat"}\n\n',
        "data: [DONE]\n\n",
    ]
    orchestration._runner_relay_tasks.clear()
    try:
        with caplog.at_level(logging.WARNING, logger=orchestration._logger.name):
            handle = orchestration._ensure_runner_relay(
                session_id,
                "runner-recursion",
                _RawFrameClient(frames),  # type: ignore[arg-type]
            )
            assert handle is not None
            await asyncio.wait_for(handle.ready.wait(), timeout=1.0)
            await asyncio.wait_for(handle.task, timeout=1.0)

        records = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "runner_stream_json_recursion"
        ]
        assert len(records) == 1
        assert "private recursive payload" not in records[0].getMessage()
    finally:
        orchestration._runner_relay_tasks.clear()


@pytest.mark.asyncio
async def test_relay_done_callback_logs_unexpected_exception(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected relay failures are retrieved and logged without payload text."""
    session_id = "relay-diagnostics-failure"

    async def _fail(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("synthetic relay failure")

    monkeypatch.setattr(orchestration, "_relay_runner_stream", _fail)
    orchestration._runner_relay_tasks.clear()
    try:
        with caplog.at_level(logging.WARNING, logger=orchestration._logger.name):
            handle = orchestration._ensure_runner_relay(
                session_id,
                "runner-failing",
                object(),  # type: ignore[arg-type]
            )
            assert handle is not None
            await asyncio.wait_for(_wait_until_done(handle.task), timeout=1.0)

        failures = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "runner_stream_task_failed"
        ]
        assert len(failures) == 1
        record = failures[0]
        assert record.session_id == session_id
        assert record.attributes["runner_id"] == "runner-failing"
        assert record.attributes["exception_type"] == "RuntimeError"
        assert record.exc_info is None
        assert record.attributes["failure_function"] == "_fail"
        assert record.attributes["superseded"] is False
        assert "synthetic relay failure" not in record.getMessage()
        assert orchestration._runner_relay_tasks.get(session_id) is None
    finally:
        orchestration._runner_relay_tasks.clear()


@pytest.mark.asyncio
async def test_relay_done_callback_logs_stale_failure_without_evicting_newer_task(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded relay failure is logged while the replacement stays registered."""
    session_id = "relay-diagnostics-stale"
    old_client = object()
    new_client = object()
    old_release = asyncio.Event()
    new_release = asyncio.Event()

    async def _relay(
        _session_id: str,
        runner_client: Any,
        _conversation_store: Any,
        _ready: asyncio.Event,
    ) -> None:
        if runner_client is old_client:
            try:
                await old_release.wait()
            except asyncio.CancelledError:
                await old_release.wait()
            raise RuntimeError("superseded relay failure")
        await new_release.wait()

    monkeypatch.setattr(orchestration, "_relay_runner_stream", _relay)
    orchestration._runner_relay_tasks.clear()
    old_handle = None
    new_handle = None
    try:
        with caplog.at_level(logging.WARNING, logger=orchestration._logger.name):
            old_handle = orchestration._ensure_runner_relay(
                session_id,
                "runner-old",
                old_client,  # type: ignore[arg-type]
            )
            await asyncio.sleep(0)
            new_handle = orchestration._ensure_runner_relay(
                session_id,
                "runner-new",
                new_client,  # type: ignore[arg-type]
            )
            assert old_handle is not None
            assert new_handle is not None
            assert orchestration._runner_relay_tasks.get(session_id) is new_handle
            old_release.set()
            await asyncio.wait_for(_wait_until_done(old_handle.task), timeout=1.0)

        failures = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "runner_stream_task_failed"
        ]
        assert len(failures) == 1
        assert failures[0].attributes["runner_id"] == "runner-old"
        assert failures[0].attributes["superseded"] is True
        assert failures[0].levelno == logging.WARNING
        assert orchestration._runner_relay_tasks.get(session_id) is new_handle
    finally:
        new_release.set()
        if new_handle is not None and not new_handle.task.done():
            new_handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await new_handle.task
        orchestration._runner_relay_tasks.clear()


async def _wait_until_done(task: asyncio.Task[None]) -> None:
    """Yield until a task and its done callbacks have run without retrieving its error."""
    while not task.done():
        await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["normal", "cancelled"])
async def test_relay_done_callback_skips_normal_and_cancelled_tasks(
    outcome: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expected relay exits do not produce task-failure diagnostics."""
    session_id = f"relay-diagnostics-{outcome}"
    finished = asyncio.Event()

    async def _finish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        finished.set()

    monkeypatch.setattr(orchestration, "_relay_runner_stream", _finish)
    orchestration._runner_relay_tasks.clear()
    try:
        with caplog.at_level(logging.ERROR, logger=orchestration._logger.name):
            handle = orchestration._ensure_runner_relay(
                session_id,
                f"runner-{outcome}",
                object(),  # type: ignore[arg-type]
            )
            assert handle is not None
            if outcome == "cancelled":
                handle.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await handle.task
            else:
                await asyncio.wait_for(finished.wait(), timeout=1.0)
                await handle.task
            await asyncio.sleep(0)

        assert not any(
            getattr(record, "event_name", None) == "runner_stream_task_failed"
            for record in caplog.records
        )
    finally:
        orchestration._runner_relay_tasks.clear()
