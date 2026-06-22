"""
Driver for the overflow-render regression test (test_overflow_render.py).

Streams a numbered markdown list whose total line count EXCEEDS the
PTY's viewport rows, so the streamed lines scroll past the top of the
viewport into scrollback. A buggy ``replace_streamed_text`` would
issue a cursor-up + erase that can't reach scrolled-off rows, leaving
raw markdown in scrollback while the rendered markdown also appears in
the viewport — visible duplication wherever the user can see scrollback.

The streamed text and the final markdown ``StreamReplace`` are written
synchronously by :meth:`TerminalHost.output` (plain ``print`` /
``sys.stdout.write``). We drive those calls directly, in order, instead
of running the interactive prompt-toolkit application. The app's pinned
prompt + toolbar redraw concurrently on a ~10fps animation timer and
share the same PTY, interleaving cursor moves and erases between the
driver's synchronous prints — which made the captured byte stream
nondeterministic (the test flaked with the rendered markdown AND the
raw streamed text both surviving). The overflow guard under test
(``_should_stream_more`` + the cursor-up clear in ``_replace_live_region``)
does not involve the prompt redraw at all, so driving ``output`` without
the live app exercises the exact same code path deterministically.

``_term_height`` / ``_term_width`` read the real PTY size (set by the
test's ``ioctl(TIOCSWINSZ)``), so the viewport ceiling and the
scrollback behaviour are faithful to a real terminal.

Sister to ``_double_render_driver.py``, which exercises the in-viewport
replace path.
"""

from __future__ import annotations

import sys

from omnigent_client import BlockContext, TextChunk, TextDone
from omnigent_ui_sdk.terminal._formatter import RichBlockFormatter
from omnigent_ui_sdk.terminal._host import TerminalHost


def _main() -> None:
    """
    Drive the host with a long numbered list (intentionally exceeds the
    viewport). Splits via ``RichBlockFormatter`` exactly as the REPL
    would for a streamed text response, then emits the end-of-response
    markdown ``StreamReplace``. All output is synchronous and ordered —
    no event loop, no sleeps, no concurrent prompt redraw — so the
    captured byte stream is identical on every run.
    """
    host = TerminalHost(model_name="overflow_test")
    fmt = RichBlockFormatter()
    ctx = BlockContext(agent=None, depth=0, turn=0)

    # Each item ends with a trailing space so the assertion
    # ``"description for item N "`` (with space) is unambiguous —
    # without it, "for item 1" would also match "for item 10",
    # "for item 11", etc., poisoning the count.
    chunks: list[str] = ["Items:\n"]
    for i in range(1, 30):
        chunks.append(f"{i:2}. **item{i}** — description for item {i} .\n")
    chunks.append("30. **item30** — description for item 30 .\n\nAll 30 items above.")
    full = "".join(chunks)

    for c in chunks:
        for item in fmt.format_text_chunk(TextChunk(text=c, ctx=ctx)):
            host.output(item)
    for item in fmt.format_text_done(TextDone(full_text=full, ctx=ctx)):
        host.output(item)
    sys.stdout.flush()


if __name__ == "__main__":
    _main()
    sys.exit(0)
