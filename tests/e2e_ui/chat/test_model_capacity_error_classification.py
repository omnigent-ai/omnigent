"""A turn that hits an upstream model-capacity 429 must fail with a structured reason.

If the openai-agents harness flattens the provider's ``RateLimitError`` into
wrapper strings, the failed turn persists an unclassified ``RuntimeError`` and
the chat shows the generic "<agent> ran into an error during this turn."
headline instead of the rate-limit headline that names the upstream cause and
offers a retry.
"""

from __future__ import annotations

from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
_CAPACITY_SIGNATURE = "Selected model is at capacity"
# Content matching routes the SDK's retries to the same scripted outage.
_TRIGGER = "Summarize the release notes for me"
# Cover SDK retries and a concurrent title-generation call drawing from the same queue.
_CAPACITY_RESPONSES = 16
_RATE_LIMIT_HEADLINE = "The model's rate limit was reached. You can retry this turn."
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TURN_SETTLE_TIMEOUT_MS = 120_000


def _failure_error_item(base_url: str, session_id: str) -> dict[str, Any] | None:
    """Return the turn's persisted failure ``error`` item, if any.

    :param base_url: Live server base URL.
    :param session_id: The session/conversation id.
    :returns: The first non-info ``error`` item, or ``None``.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=10.0,
    )
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("type") != "error":
            continue
        data = item.get("data") or {}
        if str(data.get("level") or item.get("level") or "") == "info":
            continue
        return item
    return None


def test_model_capacity_429_preserves_structured_error_reason(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Sending a message while the model is at capacity fails with a classified error.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the live server.
    :param mock_llm_server_url: Mock model endpoint scripted to be at capacity.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": _CAPACITY_MESSAGE, "status_code": 429}] * _CAPACITY_RESPONSES,
        key="model-at-capacity",
        match=_TRIGGER,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    composer.fill(_TRIGGER)
    page.get_by_role("button", name="Send", exact=True).click()

    pills = page.get_by_test_id("error-pill")
    expect(pills.or_(page.locator(_ASSISTANT)).first).to_be_visible(
        timeout=_TURN_SETTLE_TIMEOUT_MS
    )
    if pills.count() == 0:
        # A turn retried through the outage has no error to classify.
        expect(page.locator(_ASSISTANT).first).to_be_visible()
        assert _failure_error_item(base_url, session_id) is None
        return

    pill = pills.first
    headline = pill.get_by_test_id("error-headline")
    expect(headline).to_be_visible()
    headline.click()
    expect(pill.get_by_test_id("error-message-content")).to_contain_text(
        _CAPACITY_SIGNATURE, timeout=15_000
    )

    error_item = _failure_error_item(base_url, session_id)
    assert error_item is not None, "the chat shows a failure pill but no error item was persisted"
    data = error_item.get("data") or {}
    code = str(data.get("code") or error_item.get("code") or "")
    message = str(data.get("message") or error_item.get("message") or "")

    assert _CAPACITY_SIGNATURE in message, (
        f"the failed turn does not carry the upstream capacity reason; "
        f"got code={code!r} message={message!r}"
    )
    assert code == "rate_limit_exceeded", (
        "a model-capacity 429 must fail the turn with the structured provider-throttle "
        f"code 'rate_limit_exceeded'; got unclassified code={code!r} "
        f"(headline={headline.inner_text()!r}, message={message!r})"
    )
    expect(headline).to_have_text(_RATE_LIMIT_HEADLINE)
