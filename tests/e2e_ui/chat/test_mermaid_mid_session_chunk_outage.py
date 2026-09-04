"""E2E: mermaid diagrams keep rendering through a mermaid-chunk outage.

Streamdown mounts each ``mermaid`` fence behind ``React.lazy(() =>
import('./mermaid-<hash>.js'))`` — a facade chunk that only re-exports a
component the static bundle already carries. Fetching that facade at render
time meant a failed fetch (a tab held open across a redeploy whose old hashed
chunk no longer exists, or a passing network blip) degraded the message to the
``MarkdownErrorBoundary`` fallback ("Could not render this markdown.") with its
raw source — and because ``React.lazy`` caches a rejected import forever, every
diagram for the rest of the session degraded too, until a full page reload.

The facade is pinned into the static bundle by
``web/src/components/ai-elements/eagerMermaidFacade.ts``, so a diagram render
never fetches a mermaid chunk: a valid diagram arriving while the chunk
endpoint is failing must render, and so must every diagram after the outage
passes. The route-abort below is the faithful stand-in for the redeploy / blip.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Locator, Page, Route, expect

_AGENT_NAME = "hello_world"
# Every emitted mermaid chunk, so this keeps matching across rebuilds (the
# hashes change on every build).
_MERMAID_CHUNKS = "**/mermaid-*.js"
_FALLBACK_TEXT = "Could not render this markdown."

_FIRST_MARKER = "First diagram arrives during the network blip."
_SECOND_MARKER = "Second diagram arrives on a healthy network."


def _mermaid_message(marker: str) -> str:
    """A valid flowchart fence with an identifying line of prose above it."""
    return (
        f"{marker}\n\n"
        "```mermaid\n"
        "flowchart LR\n"
        "  A[Client] --> B[Server]\n"
        "  B --> C[(Database)]\n"
        "```\n"
    )


def _post_assistant_message(base_url: str, session_id: str, response_id: str, text: str) -> None:
    """Deliver one assistant message to the session (arrives live over SSE)."""
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "response_id": response_id, "text": text},
        },
        timeout=10.0,
    ).raise_for_status()


def _diagram_in(section: Locator) -> Locator:
    """The rendered mermaid SVG inside one assistant message section.

    Mermaid stamps its output with aria-roledescription (e.g. "flowchart-v2"),
    which distinguishes the diagram from the block's own control icons.
    """
    return section.locator('[data-streamdown="mermaid-block"] svg[aria-roledescription]')


def test_diagrams_render_during_and_after_chunk_outage(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Diagram messages render while mermaid chunks are unfetchable, and after."""
    base_url, session_id = seeded_session

    # Mid-session: the page is open and healthy before anything fails.
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)

    # The blip: no mermaid chunk can be fetched from here on.
    page.route(_MERMAID_CHUNKS, lambda route: Route.abort(route, "failed"))
    _post_assistant_message(
        base_url, session_id, "resp_diagram_blip", _mermaid_message(_FIRST_MARKER)
    )

    # The diagram must render anyway: nothing on the render path fetches a
    # mermaid chunk anymore, so the outage cannot degrade the message to the
    # raw-source fallback.
    first = page.locator('[data-testid="assistant-text-section"]', has_text=_FIRST_MARKER)
    expect(first).to_be_visible(timeout=30_000)
    expect(_diagram_in(first)).to_be_visible(timeout=30_000)
    expect(first.get_by_text(_FALLBACK_TEXT)).not_to_be_visible()

    # The blip passes, the user keeps chatting in the same tab: later diagrams
    # must render too (no cached rejection poisoning the rest of the session).
    page.unroute(_MERMAID_CHUNKS)
    _post_assistant_message(
        base_url, session_id, "resp_diagram_healthy", _mermaid_message(_SECOND_MARKER)
    )

    second = page.locator('[data-testid="assistant-text-section"]', has_text=_SECOND_MARKER)
    expect(second).to_be_visible(timeout=30_000)
    expect(_diagram_in(second)).to_be_visible(timeout=15_000)

    # The session never saw the degraded state at all.
    expect(page.get_by_text(_FALLBACK_TEXT)).to_have_count(0)
