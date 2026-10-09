"""A model-capacity 429 must fail the turn as a classified rate-limit error.

Production sessions log ``session turn failed for <id>: Selected model is at
capacity. Please try a different model.`` — an upstream model-serving
throttle (HTTP 429), not an Omnigent defect. The failed turn has to keep that
structured reason: the chat pill names the rate limit and the persisted
``error`` item carries ``rate_limit_exceeded``, so the failure is not counted
as an unexplained Omnigent error. The mock endpoint refuses every call, so a
turn that completes anyway means the scripted refusals ran out before the
harness gave up; the test fails instead of skipping its assertions.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm

_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
_CAPACITY_SIGNATURE = "Selected model is at capacity"
_TRIGGER = "Summarize the release notes for me"
# The provider SDK retries 429s itself and title generation matches the same
# text; queue enough refusals that every attempt of this turn sees the outage.
_CAPACITY_RESPONSES = 16
_RATE_LIMIT_HEADLINE = "The model's rate limit was reached. You can retry this turn."
_TURN_SETTLE_TIMEOUT_S = 90.0


def _error_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Return the error fields of a listed item, which the API flattens."""
    nested = item.get("data")
    return nested if isinstance(nested, dict) else item


def _settled_turn_error(base_url: str, session_id: str) -> dict[str, Any] | None:
    """Poll the transcript until the turn settles.

    :param base_url: Server base URL.
    :param session_id: The session/conversation id.
    :returns: The persisted failure ``error`` item's fields, or ``None`` when
        an assistant reply landed instead.
    :raises AssertionError: If neither is persisted within the timeout.
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
            fields = _error_fields(item)
            if item.get("type") == "error" and str(fields.get("level") or "") != "info":
                return fields
        for item in items:
            role = item.get("role") or _error_fields(item).get("role")
            if item.get("type") == "message" and role == "assistant":
                return None
        time.sleep(0.5)
    raise AssertionError(
        f"turn never settled within {_TURN_SETTLE_TIMEOUT_S:.0f}s: no error item "
        "and no assistant reply were persisted"
    )


def test_model_capacity_429_fails_turn_as_rate_limit(
    request: Any,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Sending a message while the model is at capacity yields a rate-limit failure.

    :param request: Pytest request; the recorded page is created after setup.
    :param seeded_session: ``(base_url, session_id)`` on the live server.
    :param mock_llm_server_url: Mock model endpoint scripted to be at capacity.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": _CAPACITY_MESSAGE, "status_code": 429}] * _CAPACITY_RESPONSES,
        key="model-at-capacity",
        match=_TRIGGER,
    )
    try:
        _drive_capacity_turn(request, base_url, session_id)
    finally:
        reset_mock_llm(mock_llm_server_url)


def _drive_capacity_turn(request: Any, base_url: str, session_id: str) -> None:
    """Send the trigger message and verify the failed turn's classification.

    :param request: Pytest request; the recorded page is created here, after setup.
    :param base_url: Server base URL.
    :param session_id: The session/conversation id.
    """
    page = request.getfixturevalue("page")
    assert isinstance(page, Page)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    composer.fill(_TRIGGER)
    page.get_by_role("button", name="Send", exact=True).click()

    error = _settled_turn_error(base_url, session_id)
    if error is None:
        pytest.fail(
            "the turn completed instead of failing: the mock endpoint's "
            f"{_CAPACITY_RESPONSES} scripted refusals ran out before the harness gave up; "
            "raise _CAPACITY_RESPONSES"
        )

    pill = page.get_by_test_id("error-pill").first
    expect(pill).to_be_visible(timeout=15_000)
    headline = pill.get_by_test_id("error-headline")
    expect(headline).not_to_be_empty()
    pill.click()
    expect(pill).to_contain_text(_CAPACITY_SIGNATURE, timeout=10_000)
    # Hold the expanded pill so a recording of the journey stays readable.
    page.wait_for_timeout(1_500)

    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    session_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    session_resp.raise_for_status()
    last_task_error = session_resp.json().get("last_task_error") or {}

    assert _CAPACITY_SIGNATURE in message, (
        f"the failed turn's error lost the upstream capacity reason; "
        f"got code={code!r} message={message!r}"
    )
    expect(headline).to_have_text(_RATE_LIMIT_HEADLINE, timeout=5_000)
    assert code == "rate_limit_exceeded", (
        "a model-capacity 429 must fail the turn with the structured code "
        f"'rate_limit_exceeded'; got unclassified code={code!r}"
    )
    assert last_task_error.get("code") == "rate_limit_exceeded", (
        f"the session's last_task_error lost the rate-limit code; got {last_task_error!r}"
    )
    assert _CAPACITY_SIGNATURE in str(last_task_error.get("message") or ""), (
        f"the session's last_task_error lost the upstream capacity reason; got {last_task_error!r}"
    )
