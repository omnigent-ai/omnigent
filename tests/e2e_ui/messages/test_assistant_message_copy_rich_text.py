"""E2E: copying an assistant reply offers rendered HTML, not just markdown.

Pasting an agent's answer into Slack or a doc used to land literal markdown —
`**bold**` and pipe-delimited table rows — because Copy wrote only the markdown
source. The action now writes two clipboard flavors, so a rich-text target
takes ``text/html`` and keeps the formatting while a plain-text target still
gets the markdown it always got.

A deterministic assistant message (seeded via ``external_assistant_message`` —
no LLM run) carries a heading, bold text, inline code, a list and a GFM table.
Asserted, by reading both flavors back off the real browser clipboard:

  - ``text/plain`` is the markdown source, unchanged.
  - ``text/html`` carries the rendered elements a rich-text paste consumes,
    and none of the raw markdown punctuation that was the bug.
  - ``text/html`` carries no image and no app-relative href, matching what the
    chat renderer itself refuses to load.

Selectors:
  - assistant bubble: ``data-testid="message-bubble"`` + ``data-role="assistant"``
  - copy button: accessible name "Copy" exactly (MessageAction sr-only); exact
    match avoids colliding with the sibling "Copy link" deep-link control
  - copied state: lucide check icon (``svg.lucide-check``) replaces the copy
    icon (``svg.lucide-copy``) for ~2s after a successful write
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, expect

_AGENT_NAME = "hello_world"
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

# Every construct whose formatting is lost by a plain-markdown paste. Kept free
# of leading/trailing blank lines: the copy handler trims what it collects.
_MESSAGE_TEXT = "\n".join(
    [
        "## Findings",
        "",
        "A **bold** claim and `code`.",
        "",
        "- first",
        "- second",
        "",
        "| key | value |",
        "| --- | --- |",
        "| rows | 2 |",
        "",
        "A line a<br>line b break.",
        "",
        "![beacon](https://blocked.invalid/pixel.png)",
        "",
        "See [src/App.tsx](src/App.tsx).",
    ]
)

# Read every flavor the clipboard holds, not just text, so the assertions can
# tell a rich-text paste from a plain one.
_READ_FLAVORS = """
async () => {
  const items = await navigator.clipboard.read();
  const out = {};
  for (const item of items) {
    for (const type of item.types) {
      out[type] = await (await item.getType(type)).text();
    }
  }
  return out;
}
"""


@pytest.fixture
def replied_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a runner-bound session with an assistant reply carrying markdown.

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


def test_assistant_copy_offers_rendered_html_and_markdown(
    browser: Browser,
    replied_session: tuple[str, str],
) -> None:
    """Copy writes rendered HTML beside the markdown source.

    A failure means the rich-text copy regressed: either the HTML flavor is
    missing (a rich-text paste falls back to literal markdown again), the
    markdown flavor changed (breaking paste into an editor), or the copied-state
    icon swap broke.

    Clipboard read/write needs the ``clipboard-read``/``clipboard-write``
    permissions, granted on a dedicated context here so the default
    function-scoped ``page`` fixture stays untouched.
    """
    base_url, session_id = replied_session

    ctx = browser.new_context()
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        bubble = page.locator(_ASSISTANT_BUBBLE).filter(has_text="Findings").first
        expect(bubble).to_be_visible(timeout=30_000)

        copy_button = bubble.get_by_role("button", name="Copy", exact=True)
        # Hover-reveal only changes opacity, so the button is always in the tree.
        expect(copy_button.locator("svg.lucide-copy")).to_have_count(1, timeout=30_000)

        copy_button.click()

        # The write is async; the icon swaps to a check on success.
        expect(copy_button.locator("svg.lucide-check")).to_have_count(1, timeout=5_000)

        flavors = page.evaluate(_READ_FLAVORS)

        # Plain text is still exactly the markdown, so pasting into an editor
        # or a terminal is unchanged.
        assert flavors.get("text/plain") == _MESSAGE_TEXT

        # The HTML flavor is what Slack and Google Docs consume.
        html = flavors.get("text/html")
        assert html, f"no text/html flavor on the clipboard; got {sorted(flavors)}"
        for fragment in ("<h2", "<strong>bold</strong>", "<code>code</code>", "<li", "<table"):
            assert fragment in html, f"{fragment!r} missing from the HTML flavor: {html!r}"

        # The bug itself: raw markdown punctuation reaching a rich-text paste.
        for raw in ("**bold**", "## Findings", "| --- |"):
            assert raw not in html, f"{raw!r} leaked into the HTML flavor: {html!r}"

        # Inline HTML renders, so neighbouring words keep their break. The
        # clipboard round-trip re-serializes, so match the tag, not its spelling.
        assert "<br" in html, f"the <br> was dropped: {html!r}"
        assert "aline b" not in html, f"the <br> fused its neighbours: {html!r}"

        # Chat blocks remote images in agent output; carrying one onto the
        # clipboard would load it on paste, in an app with no such block.
        assert "<img" not in html, f"an image reached the clipboard: {html!r}"
        assert "blocked.invalid" not in html, f"a remote src reached the clipboard: {html!r}"

        # A workspace citation resolves against the paste target, not this app,
        # so the anchor goes and the path stays readable.
        assert 'href="src/App.tsx"' not in html, f"an in-app href survived: {html!r}"
        assert "src/App.tsx" in html, f"the cited path was lost: {html!r}"
    finally:
        ctx.close()
