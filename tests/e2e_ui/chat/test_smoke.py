"""Smoke test: build SPA, spawn server, send a message, see a response.

This is the bootstrap test for ``tests/e2e_ui/`` — it verifies the
full chain (Vite build → static mount → React boot → SSE stream →
DOM render) is wired correctly. It is intentionally minimal; richer
coverage (multi-turn, refresh hydration, stop-cancel, deep-link
errors) lands in follow-up tests once stable selectors are in place.

Selectors are accessibility-first where they're stable: the textarea
is found by its placeholder, the Send button by its accessible name
(a hidden ``<span class="sr-only">Send</span>`` per
``ap-web/src/pages/ChatPage.tsx``). Real message bubbles use
``data-testid="message-bubble"`` + ``data-role={user|assistant}``.
Without the testid we can't distinguish the streaming "Working…"
shimmer (also rendered as ``<Message from="assistant">``) from a
real assistant bubble — the test would pass on the spinner.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def test_send_message_renders_assistant_response(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """
    Open a pre-created session, type a prompt, click Send, and assert
    an assistant bubble renders with non-empty text.

    A failure here means one of:

    - The SPA didn't boot (build missing or static mount broken in
      ``omnigent/server/app.py``'s ``_SPAStaticFiles``).
    - The composer is mis-wired (``ChatPage.tsx`` regression).
    - The agent never received the request (server / runtime
      regression — check the live_server log).
    - The LLM never responded (Databricks credentials missing or
      hello_world model unavailable).
    - The SDK reducer didn't render output (TS reducer parity drift
      vs ``omnigent_client/_stream.py`` — see
      ``ap-web/README.md`` § Reducer parity).

    Starts from ``/c/<id>`` rather than ``/`` because the home route
    no longer renders a composer — see :func:`seeded_session`.

    :param page: Playwright page fixture (function-scoped, fresh
        browser context per test).
    :param seeded_session: ``(base_url, session_id)`` from the
        pre-created session fixture.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill("Say 'pong' in one word.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The URL should already match /c/<id> since we started there.
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}"))

    # Wait for a real assistant bubble (NOT the "Working…" shimmer
    # — that has data-testid="working-indicator" instead). 60s budget
    # covers cold-start LLM latency under Databricks routing without
    # masking a true hang. ``re.compile(r"\S")`` matches any non-
    # whitespace character — a rendered bubble whose MessageContent
    # is empty would mean the streaming reducer fired but produced
    # no text, itself a regression worth surfacing.
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)
