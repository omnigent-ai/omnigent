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
async def test_callback_failure_identifies_callable_without_its_arguments(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    future.cancel()
    handle = asyncio.Handle(
        functools.partial(future.set_result, "private-callback-value"), (), loop
    )
    monkeypatch.setattr(_entry, "runner_primary_session_id", lambda: "session-example")

    with caplog.at_level(logging.ERROR, logger=_entry.__name__):
        _entry._handle_loop_exception(
            loop,
            {
                "message": "Exception in callback with private-context-value",
                "exception": asyncio.InvalidStateError("invalid state"),
                "handle": handle,
                "future": future,
            },
        )

    row = record_to_row(caplog.records[-1], source="runner")
    assert row["session_id"] == "session-example"
    assert row["event_name"] == "runner_async_failure"
    assert row["attributes"]["callback_name"] == "Future.set_result"
    assert row["attributes"]["context_kind"] == "callback"
    assert row["attributes"]["exception_type"] == "InvalidStateError"
    assert row["attributes"]["future_done"] == "True"
    assert row["attributes"]["future_cancelled"] == "True"
    assert "private-callback-value" not in json.dumps(row)
    assert "private-context-value" not in json.dumps(row)


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
