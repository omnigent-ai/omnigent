"""The shared poll helpers check the condition once more before failing."""

from __future__ import annotations

import asyncio

import pytest

from tests._helpers.polling import poll_until, poll_until_async


def test_poll_until_accepts_a_condition_that_holds_at_the_deadline() -> None:
    poll_until(lambda: True, timeout_s=0)


def test_poll_until_async_accepts_a_condition_that_holds_at_the_deadline() -> None:
    asyncio.run(poll_until_async(lambda: True, timeout_s=0))


def test_poll_until_fails_when_the_condition_never_holds() -> None:
    with pytest.raises(AssertionError, match="condition not met within 0s"):
        poll_until(lambda: False, timeout_s=0)


def test_poll_until_async_fails_when_the_condition_never_holds() -> None:
    with pytest.raises(AssertionError, match="condition not met within 0s"):
        asyncio.run(poll_until_async(lambda: False, timeout_s=0))
