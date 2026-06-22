"""
Minimal TerminalHost driver for the overlay ``/`` search test
(:mod:`tests.unit.frontends.sdk.test_overlay_search`).

Boots a :class:`TerminalHost` with a single overlay whose
builder returns a long, structured block of lines — enough that
the test can press ``/`` + a distinctive needle and assert the
content pane scrolled to the matching line.

Run directly with ``python _overlay_search_driver.py`` — the
main loop blocks until Ctrl+D / Ctrl+C.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from omnigent_ui_sdk import Overlay, OverlayTarget, TerminalHost

# A distinctive needle the test searches for. Deliberately
# lowercase so we also verify case-insensitive matching (the
# test types an uppercase variant).
_NEEDLE = "purple-badger-foxtrot"

# A decoy string that appears ABOVE the needle in the content,
# so the test can prove the search actually scrolled (if the
# pane were still at the top, the decoy would be visible but
# the needle — far down the list — would not be).
_DECOY = "aaa-top-of-content-zzz"


def _build_content_text() -> str:
    """
    Build the overlay's static content block.

    Structure: top banner with the decoy + 80 filler lines +
    the needle + 20 more filler lines. The test uses pyte with
    40 rows, so the needle is scrolled off by default and the
    search must actually move the viewport to match it.

    :returns: The full plaintext content.
    """
    lines: list[str] = [f"=== {_DECOY} ==="]
    lines.extend(f"filler-line-{i:03d}" for i in range(80))
    lines.append(f"*** match: {_NEEDLE} ***")
    lines.extend(f"tail-line-{i:03d}" for i in range(20))
    return "\n".join(lines)


async def _build_content(target: OverlayTarget | None) -> str:
    """
    Return the static content block.

    :param target: Selected overlay target — ignored here (this
        driver only has the main target).
    :returns: The content string.
    """
    return _build_content_text()


async def _build_targets() -> list[OverlayTarget]:
    """
    Return a single static target.

    :returns: One main target — no Tab switching needed for the
        search test.
    """
    return [OverlayTarget(key="main", label="main", icon="M")]


async def _noop_handler(text: str, files: list[Any]) -> None:
    """
    Do nothing — the driver never processes input.

    Both params are required by the :class:`TerminalHost.run`
    handler contract (``Callable[[str, list[Any]], Awaitable]``);
    we accept and ignore them.

    :param text: Submitted prompt text (ignored).
    :param files: Attached files (ignored).
    """
    return


async def _main() -> None:
    """
    Boot the host and register the search-test overlay.

    Uses a throwaway history file under ``/tmp`` so the test
    run doesn't pollute the developer's personal history.
    """
    history_path = Path(tempfile.mkdtemp(prefix="overlay-search-test-")) / "history"
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
