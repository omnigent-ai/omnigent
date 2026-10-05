"""Shared API helpers for unread-session browser journeys."""

from __future__ import annotations

from playwright.sync_api import Page


def append_assistant_message(page: Page, base_url: str, session_id: str, text: str) -> None:
    """Append one visible assistant message through the authenticated events API."""
    response = page.request.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        data={
            "type": "external_assistant_message",
            "data": {"agent": "hello_world", "text": text},
        },
    )
    assert response.ok, response.text()
