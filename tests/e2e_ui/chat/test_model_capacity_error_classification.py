"""E2E: a model-capacity 429 must keep its structured error reason.

Production sessions fail turns with ``session turn failed for <id>: Selected
model is at capacity. Please try a different model.`` — an *upstream*
model-serving capacity condition (HTTP 429), not an Omnigent defect. On the
current build the harness flattens the provider's throttle error into opaque
wrapper strings: ``openai_agents_sdk_executor`` catches the SDK exception and
yields ``ExecutorError(message=f"OpenAI Agents SDK error: {exc}")``, the
executor adapter re-raises it as ``RuntimeError("inner executor error: …")``,
and the scaffold's ``_build_error_detail`` — no longer holding the original
``openai.RateLimitError`` — falls back to the exception class name instead of
the semantic ``rate_limit_exceeded`` code its classifiers define for provider
429s. The failed turn therefore persists an unclassified error, the SPA can
only render the generic "Something went wrong" headline, and the KPI pipeline
attributes the upstream capacity outage to Omnigent.

Journey driven here, on the real web SPA against a live server + runner with
the model endpoint at capacity:

1. open a session
2. send a message while the selected model's serving endpoint is at capacity
   (every model call answers HTTP 429 "Selected model is at capacity. Please
   try a different model.")
3. observable failure: the turn fails and the chat shows the error pill for
   the capacity failure

Regression guard (FAILS on the current build): the failed turn's persisted
``error`` item must preserve a structured upstream reason — the semantic
provider-throttle code ``rate_limit_exceeded`` with the upstream capacity
message intact — never an unclassified exception-class wrapper. A fix that
instead retries through the outage and completes the turn also passes: then
no error item is persisted and the assistant reply lands.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
# Substring the assertions key on, matching the production log signature
# (``session turn failed for <id>: Selected model is at capacity. …``).
_CAPACITY_SIGNATURE = "Selected model is at capacity"
# User text that routes the mock queue (content-matched, so the turn hits the
# scripted 429s no matter which model name the harness sends).
_TRIGGER = "Summarize the release notes for me"
# The provider SDK retries 429s internally (and the harness may add its own
# attempts); script enough consecutive 429s that every attempt of this turn —
# plus any concurrent server-side calls matching the same user text (title
# generation) — sees the capacity condition, exactly like a real outage.
_CAPACITY_RESPONSES = 16

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TURN_SETTLE_TIMEOUT_S = 90.0


def _settled_turn_outcome(base_url: str, session_id: str) -> dict[str, Any] | None:
    """Poll the transcript until the turn settles; return its error item.

    Reads ``GET /v1/sessions/{id}/items`` — the same API the SPA chat renders
    from — until the turn reaches a terminal record: a persisted failure
    ``error`` item (returned) or a completed assistant message (``None``).
    Info-level notices are not turn failures and are skipped.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The persisted failure ``error`` item, or ``None`` when the turn
        completed with an assistant reply instead.
    :raises AssertionError: If the turn never settles within the timeout.
    """
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 200, "order": "asc"},
            timeout=10.0,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        for item in items:
            if item.get("type") != "error":
                continue
            data = item.get("data") or {}
            if str(data.get("level") or item.get("level") or "") == "info":
                continue
            return item
        for item in items:
            if item.get("type") != "message":
                continue
            role = item.get("role") or (item.get("data") or {}).get("role")
            if role == "assistant":
                return None
        time.sleep(0.5)
    raise AssertionError(
        f"turn never settled within {_TURN_SETTLE_TIMEOUT_S:.0f}s: no error item "
        "and no assistant reply were persisted"
    )


def test_model_capacity_429_preserves_structured_error_reason(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A turn hit by an upstream capacity 429 must fail classified, not opaque.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :param mock_llm_server_url: Mock model endpoint scripted to be at capacity.
    :returns: None.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": _CAPACITY_MESSAGE, "status_code": 429}] * _CAPACITY_RESPONSES,
        key="model-at-capacity",
        match=_TRIGGER,
    )

    # 1. open the session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=15_000)

    # 2. send a message while the selected model is at capacity
    composer.fill(_TRIGGER)
    page.get_by_role("button", name="Send", exact=True).click()

    # 3. the turn settles: on the current build it fails on the capacity 429.
    error_item = _settled_turn_outcome(base_url, session_id)

    if error_item is None:
        # Fix-world alternative: the capacity outage was retried through and
        # the turn completed — nothing failed, nothing to classify.
        expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=15_000)
        return

    # The turn failed. The failure must be the injected capacity condition,
    # surfaced to the user through the standard error pill…
    data = error_item.get("data") or {}
    message = str(data.get("message") or error_item.get("message") or "")
    code = str(data.get("code") or error_item.get("code") or "")
    assert _CAPACITY_SIGNATURE in message, (
        f"the failed turn's error does not carry the upstream capacity reason; "
        f"got code={code!r} message={message!r}"
    )
    # (each failed delivery attempt renders its own pill; one is enough here)
    expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=15_000)

    # …and it must keep a structured upstream attribution: the semantic
    # provider-throttle code the SDK classifiers define for HTTP 429
    # (``rate_limit_exceeded``), never an unclassified exception-class
    # wrapper (``RuntimeError`` / ``executor_error``) that relabels the
    # upstream outage as an Omnigent defect. This assertion FAILS today.
    assert code == "rate_limit_exceeded", (
        "a model-capacity 429 must fail the turn with the structured "
        "provider-throttle code 'rate_limit_exceeded'; got unclassified "
        f"code={code!r} (message={message!r})"
    )
