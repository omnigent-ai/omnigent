"""Playwright browser lifecycle + shared UI selectors for the UI benchmark.

:class:`UIDriver` owns the async Chromium instance for a whole benchmark run.
Journeys ask it for a page under one of two isolation modes:

- ``fresh_context`` — a brand-new :class:`BrowserContext` (own cache + storage)
  per repetition, so cold-visit journeys (landing load, first token) measure a
  true first paint with nothing warm.
- ``shared_page`` — one long-lived page reused across a journey's reps, so
  journeys that depend on surviving client-side JS state (session switching via
  the sidebar, forking) keep the SPA's in-memory store between reps.

The module also centralises the ``data-testid`` / accessibility selectors the
journeys use and the browser-timing readout (Navigation Timing + First
Contentful Paint), so a UI change lands in one place.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

# ── selectors (shared by journeys) ───────────────────────────
# Landing "new chat" composer (web/src/shell/NewChatDialog.tsx).
LANDING_INPUT = '[data-testid="new-chat-landing-input"]'
LANDING_SUBMIT = '[data-testid="new-chat-landing-submit"]'
# In-session chat composer (web/src/pages/ChatPage.tsx) — accessibility-first.
COMPOSER_PLACEHOLDER = "Ask the agent anything…"
SEND_BUTTON_NAME = "Send"
# A real assistant message bubble — NOT the streaming "Working…" shimmer, which
# renders with data-testid="working-indicator" (web/src/pages/ChatPage.tsx).
ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
WORKING_INDICATOR = '[data-testid="working-indicator"]'
# Per-message fork action + the fork dialog's submit (web/src/shell/*).
FORK_FROM_RESPONSE = '[data-testid="fork-from-response"]'
FORK_SUBMIT = '[data-testid="fork-session-submit"]'


def sidebar_session_link(session_id: str) -> str:
    """CSS for a session's sidebar row link (client-side nav target).

    The sidebar renders each conversation as ``<Link to={`/c/${id}`}>`` (see
    ``web/src/shell/Sidebar.tsx``), so clicking this anchor navigates
    client-side — preserving the SPA's JS module state, unlike ``page.goto``.
    """
    return f'a[href="/c/{session_id}"]'


# In-browser performance metrics read after a navigation-based journey. Kept as
# a secondary signal only — the primary latency number is the perf_counter span
# around the awaited stop-assertion.
_BROWSER_TIMING_JS = """
() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paints = performance.getEntriesByType('paint');
  const fcp = paints.find((p) => p.name === 'first-contentful-paint');
  if (!nav) return null;
  return {
    dom_content_loaded_ms: nav.domContentLoadedEventEnd,
    load_event_ms: nav.loadEventEnd,
    response_end_ms: nav.responseEnd,
    first_contentful_paint_ms: fcp ? fcp.startTime : null,
  };
}
"""


async def read_browser_timing(page: Page) -> dict[str, float] | None:
    """Read Navigation Timing + FCP for the page's current document.

    :param page: A page that has completed a ``goto`` navigation.
    :returns: A dict of millisecond metrics, or ``None`` when the browser
        exposes no navigation entry (e.g. after a client-side-only transition).
    """
    return await page.evaluate(_BROWSER_TIMING_JS)


@dataclass
class LaunchOptions:
    """Chromium launch knobs for the run.

    :param headed: Launch a visible browser (local debugging only). CI always
        runs headless.
    :param no_sandbox: Add ``--no-sandbox`` / ``--disable-dev-shm-usage`` — set
        by the ``OMNIGENT_PW_NO_SANDBOX`` env var for root/container runners
        (matches the e2e_ui suite's launch args).
    """

    headed: bool = False
    no_sandbox: bool = False

    @classmethod
    def from_env(cls, *, headed: bool) -> LaunchOptions:
        """Build options from the CLI ``--headed`` flag + environment."""
        return cls(headed=headed, no_sandbox=bool(os.environ.get("OMNIGENT_PW_NO_SANDBOX")))


class UIDriver:
    """Async context manager owning Playwright + a Chromium browser.

    One instance spans the whole run; journeys borrow contexts/pages from it.
    """

    def __init__(self, base_url: str, options: LaunchOptions) -> None:
        self.base_url = base_url
        self._options = options
        self._pw = None
        self._browser: Browser | None = None
        self._contexts: list[BrowserContext] = []

    async def __aenter__(self) -> UIDriver:
        self._pw = await async_playwright().start()
        args: list[str] = []
        if self._options.no_sandbox:
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        self._browser = await self._pw.chromium.launch(
            headless=not self._options.headed,
            args=args,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        for ctx in self._contexts:
            with contextlib.suppress(Exception):  # teardown best-effort
                await ctx.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def new_context(self) -> BrowserContext:
        """Open a fresh browser context (own cache/storage), tracked for cleanup."""
        assert self._browser is not None
        ctx = await self._browser.new_context(base_url=self.base_url)
        self._contexts.append(ctx)
        return ctx

    async def close_context(self, ctx: BrowserContext) -> None:
        """Close and untrack a context opened via :meth:`new_context`."""
        try:
            await ctx.close()
        finally:
            if ctx in self._contexts:
                self._contexts.remove(ctx)
