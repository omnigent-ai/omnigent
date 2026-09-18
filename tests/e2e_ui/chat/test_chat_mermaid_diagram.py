"""E2E: mermaid diagrams in chat render, an un-renderable one degrades, a diagram
Mermaid cannot parse names the failing line, and punctuation semicolons in a
sequence diagram are escaped so it renders anyway.

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

# A ``;`` inside a Note ends the statement in Mermaid's grammar, so the rest of
# the line is read as an actor with no arrow: the most common LLM-authored slip.
# The bad arrow on the next line keeps the diagram unrenderable even once the
# semicolon is escaped, so this exercises the error card.
_INVALID_MERMAID_MESSAGE = (
    "Here is the sequence:\n\n"
    "```mermaid\n"
    "sequenceDiagram\n"
    "    A->>B: hi\n"
    "    Note over A,B: proceed once; do not call Save\n"
    "    A=>B: again\n"
    "```\n"
)

# The same slip on its own, next to an already-escaped ``#59;`` that must survive:
# escaping the bare semicolons is enough to render it.
_SEMICOLON_MERMAID_MESSAGE = (
    "Here is the flow:\n\n"
    "```mermaid\n"
    "sequenceDiagram\n"
    "    O->>O: Bind initiating user/run; check session policy\n"
    "    B->>B: Load grant#59; refresh if needed\n"
    "    G->>G: Record actor; target and outcome\n"
    "```\n"
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
    expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)
    assert page.evaluate("() => document.getElementById('root')?.children.length ?? 0") > 0

    # The message's content survives as markdown source rather than vanishing.
    expect(page.get_by_text("That is the shape of it.")).to_be_visible(timeout=30_000)


@pytest.fixture
def invalid_mermaid_chat_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed a settled assistant bubble carrying a fence Mermaid cannot parse."""
    base_url, session_id = seeded_session
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _INVALID_MERMAID_MESSAGE},
        },
        timeout=10.0,
    ).raise_for_status()
    yield (base_url, session_id)


def test_unparseable_diagram_names_the_failing_line(
    page: Page, invalid_mermaid_chat_session: tuple[str, str]
) -> None:
    """A parse error shows the offending line and the fix, not the parser's token dump."""
    base_url, session_id = invalid_mermaid_chat_session
    page.goto(f"{base_url}/c/{session_id}")

    card = page.get_by_test_id("mermaid-error")
    expect(card).to_be_visible(timeout=30_000)
    expect(card).to_contain_text("Mermaid couldn't parse line 3")
    expect(card.locator("pre > code")).to_have_text(
        "Note over A,B: proceed once; do not call Save"
    )
    expect(card).to_contain_text("#59;")
    # The raw parser message stays available, folded under Details.
    expect(card.locator("details")).to_contain_text("got 'NEWLINE'")


# A legitimate ``;`` statement separator on one line, punctuation on the next:
# recovery must keep all three interactions distinct.
_SEPARATOR_MERMAID_MESSAGE = (
    "Two styles:\n\n"
    "```mermaid\n"
    "sequenceDiagram\n"
    "    A->>B: first; B->>C: second\n"
    "    C->>D: punctuation; retry me\n"
    "```\n"
)


@pytest.fixture
def semicolon_mermaid_chat_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed a settled assistant bubble whose only diagram fault is text semicolons."""
    base_url, session_id = seeded_session
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _SEMICOLON_MERMAID_MESSAGE},
        },
        timeout=10.0,
    ).raise_for_status()
    yield (base_url, session_id)


def test_semicolon_diagram_renders_with_an_escape_note(
    page: Page, semicolon_mermaid_chat_session: tuple[str, str]
) -> None:
    """Text semicolons are escaped so the diagram renders, and a note says so."""
    base_url, session_id = semicolon_mermaid_chat_session
    page.goto(f"{base_url}/c/{session_id}")

    escaped = page.get_by_test_id("mermaid-escaped")
    diagram = escaped.locator("svg[aria-roledescription]")
    expect(diagram).to_be_visible(timeout=30_000)
    # Both the escaped punctuation and the author's own ``#59;`` read as literal semicolons.
    expect(diagram).to_contain_text("user/run; check session policy")
    expect(diagram).to_contain_text("Load grant; refresh if needed")
    expect(escaped).to_contain_text("2 semicolons escaped as #59; (first on line 2)")
    # Details keeps the source as written, entity code included.
    expect(escaped.locator("details")).to_contain_text("Load grant#59; refresh if needed")
    expect(page.get_by_test_id("mermaid-error")).to_have_count(0)


@pytest.fixture
def separator_mermaid_chat_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed a settled assistant bubble mixing a real separator with punctuation."""
    base_url, session_id = seeded_session
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _SEPARATOR_MERMAID_MESSAGE},
        },
        timeout=10.0,
    ).raise_for_status()
    yield (base_url, session_id)


def test_escape_keeps_statement_separators(
    page: Page, separator_mermaid_chat_session: tuple[str, str]
) -> None:
    """Only the punctuation semicolon is escaped; every interaction survives."""
    base_url, session_id = separator_mermaid_chat_session
    page.goto(f"{base_url}/c/{session_id}")

    escaped = page.get_by_test_id("mermaid-escaped")
    diagram = escaped.locator("svg[aria-roledescription]")
    expect(diagram).to_be_visible(timeout=30_000)
    expect(diagram.locator("text.messageText")).to_have_text(
        ["first", "second", "punctuation; retry me"]
    )
    expect(escaped).to_contain_text("one semicolon escaped as #59; (first on line 3)")
    expect(page.get_by_test_id("mermaid-error")).to_have_count(0)
