"""
Driver for the double-render regression test (test_double_render.py).

Boots a real :class:`omnigent_ui_sdk.terminal.TerminalHost` running
``host.run(handler)`` (the same entry point the REPL uses), simulates
the formatter's chunk-by-chunk output for a markdown response, and
exits cleanly. Assertions run in the parent process against the
``pyte``-rendered final screen state.

This driver is the IPC half of the test — it runs INSIDE a forked PTY
slave, so its job is just to drive the host and exit. The parent
captures the bytes the slave wrote and inspects them.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from omnigent_ui_sdk.terminal._formatter import StreamingText, StreamReplace
from omnigent_ui_sdk.terminal._host import TerminalHost
from prompt_toolkit.application import get_app
from rich.markdown import Markdown
from rich.padding import Padding

WELCOME_HINTS = ["/help help", "Ctrl+O debug", "Esc cancel", "Ctrl+C exit"]


async def _drive(host: TerminalHost) -> None:
    """
    Mimic what the formatter would do for a typical agent response —
    intro paragraph followed by a markdown table. Splits on ``\\n\\n``
    just like ``RichBlockFormatter._consume_paragraph_boundaries``.
    """
    # Wait for the prompt-toolkit app to fully start before driving.
    await asyncio.sleep(0.5)

    chunks = [
        "I'll design a workflow that exercises every tool. ",
        "Let me chain them together logically!",
        "\n\n",
        "| Name | Notes |\n|------|-------|\n",
        "| foo  | first |\n| bar  | second|\n",
    ]
    # Issue all streaming + replace operations in tight succession
    # WITHOUT inter-chunk sleeps. This is the regression-trip
    # scenario: a buggy implementation that schedules the
    # ``StreamReplace`` write through its own ``run_in_terminal``
    # (bypassing the StdoutProxy worker queue) races the streaming
    # writes and lands BEFORE them. With the correct implementation
    # (going through ``sys.stdout`` so the proxy's worker bundles
    # streaming + replace into ONE batch), the order is preserved
    # regardless of timing.
    paragraph_buf = ""
    for c in chunks:
        paragraph_buf += c
        new_text = c
        while "\n\n" in paragraph_buf:
            idx = paragraph_buf.find("\n\n")
            host.output(StreamingText(text=new_text))
            new_text = ""
            para = paragraph_buf[:idx]
            if para.strip():
                host.output(StreamReplace(renderable=Padding(Markdown(para), (0, 1, 0, 3))))
            paragraph_buf = paragraph_buf[idx + 2 :]
        if new_text:
            host.output(StreamingText(text=new_text))
    if paragraph_buf.strip():
        host.output(StreamReplace(renderable=Padding(Markdown(paragraph_buf), (0, 1, 0, 3))))
    # Now yield to the event loop and wait for the proxy's worker
    # thread to drain. With the correct implementation this is
    # enough; with a buggy bypass-the-proxy implementation the
    # streaming and the replace have already raced and the screen
    # state is broken.
    await asyncio.sleep(1.0)
    # Best-effort exit; if the app already exited, swallow.
    with contextlib.suppress(Exception):
        get_app().exit(exception=EOFError())


async def _amain() -> None:
    host = TerminalHost(model_name="resume_test", toolbar_hints=WELCOME_HINTS)

    # Hold a reference to the driver task so it isn't garbage-collected
    # while the host's input loop runs.
    driver_task = asyncio.create_task(_drive(host))  # noqa: RUF006, F841

    async def _handler(text: str) -> None:
        return None

    with contextlib.suppress(EOFError, KeyboardInterrupt):
        await host.run(_handler)


if __name__ == "__main__":
    asyncio.run(_amain())
    sys.exit(0)
