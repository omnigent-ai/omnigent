"""
Minimal TerminalHost driver for the two-press Ctrl+C exit test
(:mod:`tests.unit.frontends.sdk.test_ctrl_c`).

Boots a :class:`TerminalHost` with a no-op input handler and
prints a sentinel to stdout when the host's ``run`` loop exits
cleanly (the only way the loop returns in this driver is via
:class:`KeyboardInterrupt` — i.e. the second Ctrl+C within the
confirm window).

Run directly with ``python _ctrl_c_driver.py`` — the main loop
runs until Ctrl+C-Ctrl+C or EOF.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from omnigent_ui_sdk import TerminalHost

# Printed to stdout after the host cleanly exits via the
# two-press Ctrl+C sequence. Unusual enough that pexpect won't
# confuse it with anything the SDK itself renders.
_EXIT_SENTINEL = "CTRL_C_DRIVER_EXITED_CLEANLY_XYZZY"


async def _noop_handler(text: str, files: list[Any]) -> None:
    """
    Do nothing — the driver never actually processes input.

    :param text: Submitted prompt text (ignored).
    :param files: Attached files (ignored).
    """
    return


async def _main() -> None:
    """
    Boot the host; print the exit sentinel after the run loop
    returns so the test can assert the clean-exit path fired.
    """
    history_path = Path(tempfile.mkdtemp(prefix="ctrl-c-test-")) / "history"
    host = TerminalHost(model_name="test", history_file=str(history_path))
    async with host:
        await host.run(_noop_handler)
    print(_EXIT_SENTINEL, flush=True)


if __name__ == "__main__":
    asyncio.run(_main())
