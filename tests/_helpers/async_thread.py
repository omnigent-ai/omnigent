"""Run async browser scenarios alongside pytest-playwright's synchronous fixtures."""

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any


def run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run on a fresh thread/loop and re-raise its failures on the calling thread.

    Cancellation and interrupts must not silently turn a scenario into a pass.
    """
    captured: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            captured.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if captured:
        raise captured[0]
