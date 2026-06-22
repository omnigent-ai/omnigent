"""
Minimal TerminalHost driver for the overlay Tab-refresh regression
test (:mod:`tests.unit.frontends.sdk.test_overlay_tab_refresh`).

Boots a :class:`TerminalHost` with two static overlay targets —
``main`` and ``sub:test`` — whose content is a unique sentinel
string per target. The test script uses pexpect to press Ctrl+O
to open the overlay, then Tab to cycle, then asserts the sub
target's sentinel appears without any follow-up keystroke. This
driver is intentionally minimal: no server, no LLM, no real
content fetch, so the test hits the SDK's keybinding + invalidate
logic in isolation.

Run directly with ``python _overlay_tab_driver.py`` — the main
loop blocks until Ctrl+D / Ctrl+C.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from omnigent_ui_sdk import Overlay, OverlayTarget, TerminalHost

# Sentinels the pexpect test anchors on. Must be uncommon enough
# that they can't appear incidentally anywhere in the SDK's own
# rendering (prompt banner, footer hint text, ANSI sequences).
_MAIN_SENTINEL = "OVERLAY_CONTENT_FOR_MAIN_TARGET_XYZZY"
_SUB_SENTINEL = "OVERLAY_CONTENT_FOR_SUB_TARGET_XYZZY"


async def _build_content(target: OverlayTarget | None) -> str:
    """
    Return a per-target sentinel string with a forced async delay.

    The 200 ms sleep is critical to the regression test. In
    production, ``_build_content`` awaits an HTTP GET against the
    conversations endpoint — so by the time the builder returns,
    the Tab keypress has already triggered a prompt-toolkit
    render, and that render captured the OLD content pane. After
    the builder completes, prompt-toolkit has no idea new content
    is available unless ``app.invalidate()`` is called.

    Without the sleep, the builder is synchronous-fast — the
    coroutine completes before the keypress-triggered render even
    happens, so ``_get_visible_ansi`` reads the fresh content
    during that first render and the test passes *even without
    the invalidate() fix*, hiding the bug.

    :param target: The currently-selected overlay target. ``None``
        only happens when ``targets_builder`` returns empty, which
        this driver never does — but handle it anyway so a stale
        sidebar build doesn't crash the overlay mid-test.
    :returns: Plain text (no Rich markup) so the overlay's content
        pane renders the sentinel literally and the test can match
        on it cleanly via pyte.
    """
    await asyncio.sleep(0.2)
    if target is None or target.key == "main":
        return _MAIN_SENTINEL
    return _SUB_SENTINEL


async def _build_targets() -> list[OverlayTarget]:
    """
    Return two static targets so Tab has somewhere to go.

    :returns: One ``main`` target + one ``sub:test`` target —
        always the same list; no server round-trip.
    """
    return [
        OverlayTarget(key="main", label="main", icon="M"),
        OverlayTarget(key="sub", label="sub:test", icon="S"),
    ]


async def _noop_handler(text: str, files: list[Any]) -> None:
    """
    Do nothing — the driver only cares about key events, not
    message handling.

    :param text: Submitted prompt text (ignored).
    :param files: Attached files (ignored).
    """
    return


async def _main() -> None:
    """
    Boot the host and register the test overlay.

    Uses a throwaway history file under ``/tmp`` so the test run
    doesn't pollute the developer's personal history. Every
    invocation gets a fresh file so previous runs don't leak
    into the up-arrow buffer.
    """
    history_path = Path(tempfile.mkdtemp(prefix="overlay-tab-test-")) / "history"
    host = TerminalHost(model_name="test", history_file=str(history_path))
    host.add_overlay(
        Overlay(
            trigger="c-o",
            builder=_build_content,
            targets_builder=_build_targets,
            title=" Test overlay ",
        ),
    )
    async with host:
        await host.run(_noop_handler)


if __name__ == "__main__":
    asyncio.run(_main())
