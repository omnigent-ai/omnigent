"""E2E: chat code blocks become syntax-highlighted via the lazy Shiki path.

Regression guard for the lazy-Shiki change. Shiki's highlighter engine is no
longer eagerly bundled into the main entry chunk; the Streamdown ``code``
plugin imports it on demand the first time a fenced code block renders. This
test proves the user-facing behavior survives that deferral: a fenced code
block in an assistant message still becomes syntax-highlighted once the lazy
import resolves.

A deterministic assistant message (seeded via the ``external_assistant_message``
event — no LLM run) carries a fenced ``ts`` block. Streamdown renders each
highlighted token as its own ``<span>`` carrying a per-token color through the
``--sdm-c`` CSS custom property; the raw, pre-highlight path emits a single
uncolored line span. So the test asserts the observable behavior:

  - **After highlighting:** more than one ``span[style*="--sdm-c"]`` token span
    appears, and distinct tokens (the ``const`` keyword and the ``42`` literal)
    are among them.

Highlighting is async (the engine is lazily imported), so the token spans are
awaited with Playwright's ``expect(...).to_have_count`` / ``to_be_visible``
timeouts rather than a fixed sleep.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_CODE_BODY = '[data-streamdown="code-block-body"]'
# Streamdown emits one span per highlighted token, each carrying its Shiki color
# via the `--sdm-c` custom property. The raw (pre-highlight) path has no such
# spans, so their presence is the signal that the lazy engine tokenized.
_TOKEN_SPANS = '[data-streamdown="code-block-body"] span[style*="--sdm-c"]'

# Fenced ``ts`` block whose highlighted output splits into multiple colored
# tokens, including a distinct `const` keyword and `42` numeric literal.
_MESSAGE_TEXT = "Here is a snippet:\n\n```ts\nconst answer = 42;\n```\n"


@pytest.fixture
def highlight_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a runner-bound session with a fenced ``ts`` code-block reply.

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


def test_code_block_becomes_syntax_highlighted(
    page: Page,
    highlight_session: tuple[str, str],
) -> None:
    """A fenced code block is syntax-highlighted after the lazy Shiki import."""
    base_url, session_id = highlight_session
    page.goto(f"{base_url}/c/{session_id}")

    # The assistant bubble and its rendered code block must mount first.
    body = page.locator(_CODE_BODY).first
    expect(body).to_be_visible(timeout=30_000)

    # The lazy @streamdown/code import + tokenization resolves asynchronously and
    # re-renders the block with per-token colored spans. Wait for more than one.
    token_spans = page.locator(_TOKEN_SPANS)
    expect(token_spans.first).to_be_visible(timeout=30_000)
    page.wait_for_function(
        "() => document.querySelectorAll('" + _TOKEN_SPANS.replace("'", "\\'") + "').length > 1",
        timeout=30_000,
    )

    # Distinct tokens land in their own highlighted spans: the `const` keyword
    # and the `42` literal are both syntax-highlighted.
    expect(token_spans.filter(has_text="const").first).to_be_visible(timeout=30_000)
    expect(token_spans.filter(has_text="42").first).to_be_visible(timeout=30_000)
