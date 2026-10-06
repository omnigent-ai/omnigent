"""Worker failures must fail the caller even when its event loop is running."""

import asyncio

import pytest

from tests._helpers.async_thread import run_in_fresh_loop


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("worker failed"), asyncio.CancelledError()])
async def test_worker_failure_reaches_caller(error: BaseException) -> None:
    caller_loop = asyncio.get_running_loop()

    async def fail() -> None:
        assert asyncio.get_running_loop() is not caller_loop
        raise error

    with pytest.raises(type(error)) as caught:
        run_in_fresh_loop(fail())
    assert caught.value is error
