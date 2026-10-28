"""Runner loop diagnostics identify code without recording callback payloads."""

import asyncio
import functools
import json
import logging

import pytest
from websockets.exceptions import ConnectionClosedOK

from omnigent.debug_logging import record_to_row
from omnigent.runner import _entry


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_callback_failure_identifies_callable_without_its_arguments(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
    partial: bool,
) -> None:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    if cancelled:
        future.cancel()
    else:
        future.set_result("private-existing-result")
    callback = future.set_result
    callback_args = ("private-callback-value",)
    if partial:
        callback = functools.partial(callback, *callback_args)
        callback_args = ()
    monkeypatch.setattr(_entry, "runner_primary_session_id", lambda: "session-example")
    handled = asyncio.Event()
    previous_handler = loop.get_exception_handler()

    def handle_error(loop, context):
        _entry._handle_loop_exception(loop, context)
        handled.set()

    loop.set_exception_handler(handle_error)
    try:
        with caplog.at_level(logging.ERROR, logger=_entry.__name__):
            loop.call_soon(callback, *callback_args)
            await asyncio.wait_for(handled.wait(), timeout=1)
    finally:
        loop.set_exception_handler(previous_handler)

    row = record_to_row(caplog.records[-1], source="runner")
    assert row["session_id"] == "session-example"
    assert row["event_name"] == "runner_async_failure"
    assert row["attributes"]["callback_name"] == "Future.set_result"
    assert row["attributes"]["context_kind"] == "callback"
    assert row["attributes"]["exception_type"] == "InvalidStateError"
    assert row["attributes"]["future_done"] == "True"
    assert row["attributes"]["future_cancelled"] == str(cancelled)
    assert "private-callback-value" not in json.dumps(row)
    assert "private-existing-result" not in json.dumps(row)


@pytest.mark.asyncio
async def test_explicit_context_future_takes_precedence_over_callback_receiver(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = asyncio.get_running_loop()
    context_future = loop.create_future()
    callback_future = loop.create_future()
    callback_future.cancel()
    handle = asyncio.Handle(callback_future.set_result, ("private-callback-value",), loop)

    with caplog.at_level(logging.ERROR, logger=_entry.__name__):
        _entry._handle_loop_exception(
            loop,
            {
                "exception": asyncio.InvalidStateError("invalid state"),
                "handle": handle,
                "future": context_future,
            },
        )

    row = record_to_row(caplog.records[-1], source="runner")
    assert row["attributes"]["future_done"] == "False"
    assert row["attributes"]["future_cancelled"] == "False"


@pytest.mark.asyncio
async def test_task_failure_identifies_coroutine_without_task_name_or_locals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def load_session(secret: str) -> None:
        raise RuntimeError("synthetic background failure")

    task = asyncio.create_task(load_session("private-local-value"), name="private-task-name")
    with pytest.raises(RuntimeError, match="synthetic background failure"):
        await task
    with caplog.at_level(logging.ERROR, logger=_entry.__name__):
        _entry._handle_loop_exception(
            asyncio.get_running_loop(), {"future": task, "exception": task.exception()}
        )

    row = record_to_row(caplog.records[-1], source="runner")
    assert row["attributes"]["coroutine_name"].endswith(".<locals>.load_session")
    assert row["attributes"]["context_kind"] == "task"
    assert row["attributes"]["exception_type"] == "RuntimeError"
    assert row["attributes"]["future_done"] == "True"
    assert "private-local-value" not in json.dumps(row)
    assert "private-task-name" not in json.dumps(row)


@pytest.mark.asyncio
async def test_eager_task_without_retained_coroutine_is_still_classified_as_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class CompletedTaskWithoutCoroutine(asyncio.Task[None]):
        def get_coro(self) -> None:
            return None

    async def fail_immediately() -> None:
        raise RuntimeError("synthetic eager failure")

    loop = asyncio.get_running_loop()
    task = CompletedTaskWithoutCoroutine(fail_immediately(), loop=loop)
    with pytest.raises(RuntimeError, match="synthetic eager failure"):
        await task
    with caplog.at_level(logging.ERROR, logger=_entry.__name__):
        _entry._handle_loop_exception(loop, {"task": task, "exception": task.exception()})

    row = record_to_row(caplog.records[-1], source="runner")
    assert row["attributes"]["context_kind"] == "task"
    assert "coroutine_name" not in row["attributes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), ConnectionClosedOK(None, None)])
async def test_expected_teardown_stays_at_debug(
    caplog: pytest.LogCaptureFixture, error: BaseException
) -> None:
    with caplog.at_level(logging.DEBUG, logger=_entry.__name__):
        _entry._handle_loop_exception(asyncio.get_running_loop(), {"exception": error})

    assert caplog.records[-1].levelno == logging.DEBUG
    row = record_to_row(caplog.records[-1], source="runner")
    assert row["attributes"]["exception_type"] == type(error).__name__


@pytest.mark.asyncio
async def test_context_without_exception_is_not_serialized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger=_entry.__name__):
        _entry._handle_loop_exception(
            asyncio.get_running_loop(), {"message": "private-message", "payload": "private-data"}
        )

    row = record_to_row(caplog.records[-1], source="runner")
    assert caplog.records[-1].levelno == logging.ERROR
    assert row["attributes"]["context_kind"] == "other"
    assert "private-message" not in json.dumps(row)
    assert "private-data" not in json.dumps(row)
