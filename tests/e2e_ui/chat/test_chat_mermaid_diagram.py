"""E2E: mermaid diagrams in chat render, and an un-renderable one degrades.

Streamdown renders mermaid diagrams behind ``React.lazy``. Suspense catches a
*pending* import, not a failed one — a rejected lazy import is re-thrown on
every subsequent render, so without an error boundary above it React unmounts
the whole tree and the user sees a blank page instead of one broken diagram.

The second test aborts the mermaid chunk request, which is the faithful stand-in
for the real-world trigger: a tab holding a stale ``index.html`` after the SPA is
rebuilt, whose hashed mermaid chunk no longer exists on the server.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

_AGENT_NAME = "hello_world"
# Every emitted mermaid chunk, so this keeps matching across rebuilds (the
# hashes change on every build).
_MERMAID_CHUNKS = "**/mermaid-*.js"

# Trailing prose after the fence: proves the surrounding message survives.
_MERMAID_MESSAGE = (
    "Here is the architecture:\n\n"
    "```mermaid\n"
    "flowchart LR\n"
    "  A[Client] --> B[Server]\n"
    "  B --> C[(Database)]\n"
    "```\n\n"
    "That is the shape of it.\n"
)


@pytest.fixture
def mermaid_chat_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a settled assistant bubble carrying a mermaid fence (no LLM turn)."""
    base_url, session_id = seeded_session
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _MERMAID_MESSAGE},
        },
        timeout=10.0,
    ).raise_for_status()
    yield (base_url, session_id)


def test_mermaid_fence_renders_as_a_diagram(
    page: Page, mermaid_chat_session: tuple[str, str]
) -> None:
    """A mermaid fence in an assistant bubble becomes an SVG diagram."""
    base_url, session_id = mermaid_chat_session
    page.goto(f"{base_url}/c/{session_id}")

    block = page.locator('[data-streamdown="mermaid-block"]').first
    expect(block).to_be_visible(timeout=30_000)
    # Mermaid stamps its output with aria-roledescription (e.g. "flowchart-v2"),
    # which distinguishes the diagram from the block's own control icons.
    expect(block.locator("svg[aria-roledescription]")).to_be_visible(timeout=30_000)


def test_unrenderable_diagram_degrades_instead_of_blanking_the_app(
    page: Page, mermaid_chat_session: tuple[str, str]
) -> None:
    """A diagram that cannot load must not unmount the app around it."""
    base_url, session_id = mermaid_chat_session
    page.route(_MERMAID_CHUNKS, lambda route: Route.abort(route, "failed"))
    page.goto(f"{base_url}/c/{session_id}")

    # The app is still mounted: the shell renders and the composer is usable.
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible(timeout=30_000)
    assert page.evaluate("() => document.getElementById('root')?.children.length ?? 0") > 0

    # The message's content survives as markdown source rather than vanishing.
    expect(page.get_by_text("That is the shape of it.")).to_be_visible(timeout=30_000)
