"""
Minimal TerminalHost driver for the overlay auto-refresh
regression test (:mod:`tests.unit.frontends.sdk.test_overlay_refresh`).

Boots a :class:`TerminalHost` with a single overlay target whose
builder returns content that changes every invocation. This lets
the test verify that the SDK's 500 ms periodic refresh loop picks
up content changes without any user action — the exact mechanic
that keeps the debug pane live while a turn streams server-side.

The shared ``_TICK_COUNTER`` increments each builder call so the
rendered text is always distinct, and the test asserts that the
second tick's sentinel appears on-screen within the refresh
budget.

Run directly with ``python _overlay_refresh_driver.py`` — the
main loop blocks until Ctrl+D / Ctrl+C.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from omnigent_ui_sdk import Overlay, OverlayTarget, TerminalHost

# Shared counter — incremented on every builder call. The test
# opens the overlay and waits for a sentinel carrying ``tick >= 2``
# which can ONLY appear if the SDK's periodic refresh loop re-
# invoked the builder after the initial open render.
_TICK_COUNTER = [0]

# Sentinel prefix — uncommon enough that it can't collide with
# SDK chrome (welcome banner, overlay title, footer hints).
_SENTINEL_PREFIX = "OVERLAY_TICK_"


async def _build_content(target: OverlayTarget | None) -> str:
    """
    Return content tagged with the current tick counter.

    Each call bumps ``_TICK_COUNTER`` and returns
    ``f"OVERLAY_TICK_{n}_XYZZY"``. The test treats the appearance
    of ``tick=2`` (or higher) as proof the SDK re-ran the builder
    on a refresh cycle, not just on the initial open.

    :param target: Selected overlay target — ignored here (this
        driver only has the ``main`` target so the content is
        the same regardless).
    :returns: Plain-text sentinel including the tick number.
    """
    _TICK_COUNTER[0] += 1
    return f"{_SENTINEL_PREFIX}{_TICK_COUNTER[0]}_XYZZY"


async def _build_targets() -> list[OverlayTarget]:
    """
    Return a single static target.

    A lone main target exercises the auto-refresh path without
    the complexity of Tab-driven target switching — the two
    behaviors are orthogonal (Tab always forces a rebuild; the
    refresh loop runs regardless), so testing them separately
    keeps each regression test focused.

    :returns: One main target.
    """
    return [OverlayTarget(key="main", label="main", icon="M")]


async def _noop_handler(text: str, files: list[Any]) -> None:
    """
    Do nothing — the driver never processes input.

    :param text: Submitted prompt text (ignored).
    :param files: Attached files (ignored).
    """
    return


async def _main() -> None:
    """
    Boot the host and register the auto-refresh overlay.

    Uses a throwaway history file under ``/tmp`` so the test run
    doesn't pollute the developer's personal history.
    """
    history_path = Path(tempfile.mkdtemp(prefix="overlay-refresh-test-")) / "history"
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
