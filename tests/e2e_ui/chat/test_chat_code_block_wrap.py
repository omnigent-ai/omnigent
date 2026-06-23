"""E2E: chat code blocks soft-wrap by default and expose a wrap toggle.

Regression guard for the code-block readability fix. Streamdown renders a
fenced code block with ``overflow-x-auto`` on the body and the inner
``<code>`` at ``white-space: pre``, so a long line forces a horizontal
scrollbar and can't be read without scrolling sideways. ``ChatCodeBlockPre``
now soft-wraps by default (``.chat-code-wrap``) and renders a "Toggle word
wrap" button that flips back to the native horizontal-scroll view.

A deterministic assistant message (seeded via the ``external_assistant_message``
event — no LLM run) carries a fenced ``markdown`` block with intentionally long
lines (plus one long unbroken token, to exercise ``overflow-wrap: anywhere``).
The test asserts the observable behavior:

  - **Default (wrapped):** the code-block body does NOT overflow horizontally
    (``scrollWidth <= clientWidth``), and the toggle reports ``aria-pressed=true``.
  - **After clicking the toggle (unwrapped):** the long lines no longer wrap, so
    the body overflows horizontally (``scrollWidth > clientWidth``) and the
    toggle reports ``aria-pressed=false``.
  - **Clicking again** restores the wrapped, non-overflowing state.

Using ``scrollWidth``/``clientWidth`` (rather than asserting a class name) keeps
the test tied to what the user actually sees — whether the content fits the
column or needs horizontal scrolling.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_CODE_BODY = '[data-streamdown="code-block-body"]'
_TOGGLE = "Toggle word wrap"

# A long, unbroken run of text (no spaces) that comfortably exceeds the chat
# column width — exercises overflow-wrap: anywhere. A repeated word keeps it
# low-entropy (so the secret scanner doesn't flag it) and obviously not a real
# token.
_LONG_WORD = "horizontalScrolling" * 14

# Fenced ``markdown`` block whose source has deliberately long lines, so it can
# only fit the column by wrapping. Generic joke content (no real identifiers).
_MESSAGE_TEXT = (
    "Here is the doc rendered as markdown source:\n\n"
    "```markdown\n"
    "# The Compendium of Coding Jokes, Programmer Puns, and Other Crimes Against Productivity\n\n"
    "Welcome, weary traveler of the call stack, to a meticulously over-engineered collection of "
    "jokes that absolutely no product manager asked for, was scoped for two story points, somehow "
    "shipped after three sprints, and is now technically considered legacy code that nobody is "
    "brave enough to refactor or delete.\n\n"
    "This document is intentionally formatted with extremely long line widths because we believe "
    "that horizontal scrolling builds character, strengthens the wrists, and prepares you "
    "emotionally for the day you open a single line of source that just keeps going like this: "
    f"{_LONG_WORD}\n"
    "```\n"
)

# JS predicates: does the code-block body overflow horizontally (beyond a 1px
# rounding tolerance)? ``_FITS`` is the wrapped state; ``_OVERFLOWS`` the scroll.
_OVERFLOWS = (
    "() => { const el = document.querySelector('"
    + _CODE_BODY
    + "'); return !!el && el.scrollWidth - el.clientWidth > 1; }"
)
_FITS = (
    "() => { const el = document.querySelector('"
    + _CODE_BODY
    + "'); return !!el && el.scrollWidth - el.clientWidth <= 1; }"
)


@pytest.fixture
def code_block_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a runner-bound session with a long-lined markdown code block reply.

    Reuses :func:`seeded_session` (a ``hello_world`` session already bound to the
    spawned runner) and appends a deterministic assistant bubble via
    ``external_assistant_message`` so no LLM turn runs.

    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    :returns: the same ``(base_url, session_id)`` after the reply is seeded.
    """
    base_url, session_id = seeded_session
    event_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _MESSAGE_TEXT},
        },
        timeout=10.0,
    )
    event_resp.raise_for_status()
    yield (base_url, session_id)


def test_chat_code_block_wraps_by_default_and_toggle_switches(
    page: Page,
    code_block_session: tuple[str, str],
) -> None:
    """Code blocks wrap by default; the toggle switches to horizontal scroll."""
    base_url, session_id = code_block_session
    page.goto(f"{base_url}/c/{session_id}")

    # The assistant bubble and its rendered code block must be present. Shiki
    # highlights asynchronously, so wait for the body element to mount.
    body = page.locator(_CODE_BODY).first
    expect(body).to_be_visible(timeout=30_000)

    # Default: wrapped — the long lines fit the column with no horizontal scroll.
    page.wait_for_function(_FITS, timeout=30_000)

    toggle = page.get_by_role("button", name=_TOGGLE)
    expect(toggle).to_be_visible(timeout=30_000)
    expect(toggle).to_have_attribute("aria-pressed", "true")

    # Toggle off: the long lines no longer wrap, so the body overflows sideways.
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    page.wait_for_function(_OVERFLOWS, timeout=10_000)

    # Toggle back on: wrapping is restored and the overflow is gone again.
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    page.wait_for_function(_FITS, timeout=10_000)
