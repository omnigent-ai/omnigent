"""Pytest entry point for the shared browser-only chat session contract."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page

from tests.browser_ui.chat.session_contract import ChatSessionContract, chat_session_handle
from tests.browser_ui.conftest import BrowserContract


@pytest.fixture
def chat_session_contract(
    page: Page,
    browser_contract: BrowserContract,
) -> Iterator[ChatSessionContract]:
    """Renderable idle chat backend with mutable history, catalog, and events."""
    yield from chat_session_handle(page, browser_contract)
