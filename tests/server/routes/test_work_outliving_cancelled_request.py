"""Work that keeps running after its request is cancelled still reports its failure."""

from __future__ import annotations

import asyncio
import logging

import pytest

from omnigent.server.routes.sessions import routes_core
from tests._helpers.polling import poll_until_async


@pytest.mark.parametrize("cancelled", ["before_failure", "as_work_fails"])
async def test_failure_of_work_outliving_its_request_is_logged(
    caplog: pytest.LogCaptureFixture, cancelled: str
) -> None:
    """A failure is logged whether the request was cancelled before it or as it happened."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("store failed")

    request = asyncio.create_task(
        routes_core._finish_despite_cancellation(work(), name="create-session")
    )
    await started.wait()
    [work_task] = [task for task in asyncio.all_tasks() if task.get_name() == "create-session"]
    with caplog.at_level(logging.WARNING, logger=routes_core._logger.name):
        if cancelled == "before_failure":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert work_task in routes_core._unattended_work
            release.set()
        else:
            # The cancellation arrives after the work failed but before the request resumes.
            work_task.add_done_callback(lambda _: request.cancel())
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
        await poll_until_async(lambda: work_task not in routes_core._unattended_work, timeout_s=5)

    [record] = [
        logged
        for logged in caplog.records
        if "failed after its request was cancelled" in logged.getMessage()
    ]
    assert record.getMessage() == "create-session failed after its request was cancelled"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
