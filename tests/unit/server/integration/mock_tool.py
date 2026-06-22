"""
Controllable mock tool for concurrency tests.

Mirrors the ``ControllableMockClient`` (mock LLM) pattern in
``tests/server/conftest.py``. A ``MockToolCall`` represents one
expected tool invocation; the test queues calls via
``add_call(...)`` and the mock fires them in order.

The critical feature is ``block=True``: the call awaits its
``block_before_response`` event before producing a result. The
test uses ``call.call_event.wait()`` to know the tool body has
been entered (i.e., the workflow reached the deterministic race
window) and then performs the concurrent action it wants to test
(cancel, parallel send, etc.). Finally, ``call.release()``
unblocks the tool body.

Without all four pieces (block + sync gate + concurrent action +
release), a "concurrency" test is a fire-and-hope test and is
fake per the testing skill (rule #3).
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockToolCall:
    """
    One expected tool invocation in a controllable mock tool's queue.

    :param result: The string the tool returns when this call fires.
        Default is a marker so test failures show whether the mock
        actually returned versus the production tool.
    :param exception: If set, the tool raises this exception
        instead of returning. Used to simulate failed-tool paths
        end-to-end.
    :param block_before_response: If set, the tool awaits this
        event before producing its result. Tests call
        ``release()`` to unblock.
    :param call_event: Set by the mock when the tool body is
        entered. Tests can ``await call_event.wait()`` to know
        the workflow reached the call site.
    :param received_arguments: Populated by the mock when the call
        fires. Holds the kwargs the production code passed in.
        ``None`` until the call is consumed.
    """

    result: str = "mock-tool-result"
    exception: BaseException | None = None
    # threading.Event (not asyncio.Event) so the test event loop
    # can ``set()`` cross-loop into DBOS's background event loop
    # where the tool body runs. asyncio.Event is loop-bound and
    # silently fails to wake awaiters across loops.
    block_before_response: threading.Event | None = None
    call_event: threading.Event = field(default_factory=threading.Event)
    received_arguments: dict[str, Any] | None = field(default=None, repr=False)

    async def wait_called(self, *, timeout: float = 10.0) -> None:
        """
        Asynchronously wait until this MockToolCall was entered.

        Bridges the underlying sync ``threading.Event`` into an
        awaitable so tests can ``await call.wait_called()``
        regardless of which loop the tool body runs on.

        :param timeout: Max seconds to wait. ``TimeoutError``
            raised if exceeded.
        """
        await asyncio.to_thread(self.call_event.wait, timeout)
        if not self.call_event.is_set():
            raise TimeoutError(
                f"MockToolCall.call_event not set within {timeout}s",
            )

    def release(self) -> None:
        """Unblock a tool body waiting on ``block_before_response``."""
        if self.block_before_response is not None:
            self.block_before_response.set()


class ControllableMockTool:
    """
    Mock async tool with per-call synchronization gates.

    Drop-in replacement for any ``@tool``-decorated function in a
    test fixture. Calls are consumed in FIFO order; once the queue
    is exhausted, every subsequent call uses a default
    auto-completing :class:`MockToolCall` so tests don't deadlock
    on unscripted invocations.

    Usage::

        tool = ControllableMockTool()
        call_1 = tool.add_call(result="first", block=True)
        # ... start the workflow that will invoke the tool ...
        await call_1.call_event.wait()  # tool entered
        # perform concurrent action while tool is blocked ...
        call_1.release()                # let the tool finish

    Returns the result as a string (matches the runner's wire
    contract). For non-string return shapes use ``result=`` with
    a JSON-encoded string.
    """

    def __init__(self) -> None:
        """Initialize an empty call queue."""
        self._queue: deque[MockToolCall] = deque()
        # Calls consumed so far; tests can read this list to assert
        # exactly how many invocations happened.
        self.received_calls: list[MockToolCall] = []

    def add_call(
        self,
        *,
        result: str = "mock-tool-result",
        exception: BaseException | None = None,
        block: bool = False,
    ) -> MockToolCall:
        """
        Queue one expected invocation of the mock tool.

        :param result: The string the tool returns. Ignored when
            ``exception`` is set.
        :param exception: If set, the tool raises this exception
            instead of returning.
        :param block: If ``True``, the tool awaits its
            ``block_before_response`` event before returning. The
            returned :class:`MockToolCall` carries that event;
            tests call ``release()`` to unblock.
        :returns: The :class:`MockToolCall` instance enqueued.
            Tests retain a reference to await ``call_event`` and
            (if blocking) call ``release()``.
        """
        call = MockToolCall(
            result=result,
            exception=exception,
            block_before_response=threading.Event() if block else None,
        )
        self._queue.append(call)
        return call

    async def __call__(self, **kwargs: Any) -> str:
        """
        Tool entry point — called as if it were an async ``@tool`` body.

        Pops the next queued call (or fabricates a default), records
        the received kwargs, signals ``call_event``, optionally
        blocks on ``block_before_response``, then either raises
        the configured exception or returns the configured result.

        :param kwargs: Whatever arguments the production code
            passes through. Recorded on the call for test inspection.
        :returns: The configured ``result`` string.
        :raises BaseException: If the call's ``exception`` is set.
        """
        call = self._queue.popleft() if self._queue else MockToolCall()
        call.received_arguments = dict(kwargs)
        self.received_calls.append(call)
        call.call_event.set()
        if call.block_before_response is not None:
            # threading.Event.wait is sync — bridge to async via
            # asyncio.to_thread so the surrounding loop yields
            # while the offloaded thread blocks.
            await asyncio.to_thread(call.block_before_response.wait)
        if call.exception is not None:
            raise call.exception
        return call.result

    def release_all(self) -> None:
        """
        Unblock every queued call's ``block_before_response``.

        Useful in test teardown so a forgotten ``release()`` doesn't
        leave the workflow waiting forever (which would manifest as
        a hung test rather than a failure).
        """
        for call in self._queue:
            call.release()
        for call in self.received_calls:
            call.release()
