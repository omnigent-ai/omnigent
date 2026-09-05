"""E2E: a mermaid fence in an assistant bubble renders as a diagram.

Streamdown mounts each ``mermaid`` fence behind ``React.lazy(() =>
import('./mermaid-<hash>.js'))``, a facade chunk that only re-exports a
component the static bundle already carries. That facade is pinned into the
static bundle (``web/src/components/ai-elements/eagerMermaidFacade.ts``), so
rendering a diagram never fetches a mermaid chunk and a mid-session chunk
outage cannot degrade it — that journey is covered by
``test_mermaid_mid_session_chunk_outage.py``. A boot-time chunk failure is not
a mermaid-specific scenario anymore: the facade loads with the app shell, and a
throw anywhere in the markdown pipeline is contained by
``MarkdownErrorBoundary`` (unit-tested next to the component).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"

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

    # The prose around the fence renders alongside the diagram.
    expect(page.get_by_text("That is the shape of it.")).to_be_visible(timeout=30_000)
