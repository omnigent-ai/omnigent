"""Per-user pages with owned contexts and caller-owned browsers and fault routes."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from playwright.async_api import Browser as AsyncBrowser
from playwright.async_api import Page as AsyncPage
from playwright.sync_api import Browser, Page


@contextmanager
def user_page(
    browser: Browser, *, headers: dict[str, str], **context_options: Any
) -> Iterator[Page]:
    """Close this user's context even if page setup or an assertion fails.

    Headers authenticate HTTP requests; callers install any required WebSocket
    authentication relay and scenario-specific faults before navigating.
    """
    context = browser.new_context(extra_http_headers=headers, **context_options)
    try:
        yield context.new_page()
    finally:
        context.close()


@asynccontextmanager
async def async_user_page(
    browser: AsyncBrowser, *, headers: dict[str, str], **context_options: Any
) -> AsyncIterator[AsyncPage]:
    """Async counterpart of user_page; the borrowed browser remains open."""
    context = await browser.new_context(extra_http_headers=headers, **context_options)
    try:
        yield await context.new_page()
    finally:
        await context.close()
