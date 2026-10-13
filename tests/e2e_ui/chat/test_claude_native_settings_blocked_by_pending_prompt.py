"""E2E: a web model/effort change refused by a pending Claude prompt must say so.

Exercise pending-permission refusal notices through the real ``claude`` CLI and
browser: the mock scripts a gated ``Write`` so Claude parks a permission prompt,
then a model/effort change is attempted from the composer. The mock catalog
exposes one model, so the model case uses the picker's ``model_override`` PATCH
directly. The chat must name the pending prompt instead of a generic runner
failure (or nothing).
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_PENDING_CARD = '[data-testid="approval-card"][data-state="pending"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_TOAST = "[data-sonner-toast]"
_GENERIC_RUNNER_FAILURE = "Request failed on the runner"
# Wording that names what is blocking the change.
_NAMES_THE_PROMPT = re.compile(r"question|permission|dialog", re.IGNORECASE)
# The recovery step, shared by the pending-prompt and dialog refusal cases.
_NAMES_THE_NEXT_STEP = re.compile(r"in the terminal|try again", re.IGNORECASE)
# The runner answers the refusal at once; the server's forward budget is 20 s.
_NOTICE_TIMEOUT_MS = 25_000


def _script_write_prompt(mock_url: str, go_token: str) -> None:
    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, "")
    write_args = json.dumps({"file_path": f"omni-pending-{go_token}.txt", "content": "probe\n"})
    configure_mock_llm(
        mock_url,
        [{"tool_calls": [{"name": "Write", "arguments": write_args}]}, {"text": "done"}],
        key="pending-prompt",
        match=go_token,
        required_tools=["Write"],
    )


def _decline_pending(base_url: str, session_id: str) -> None:
    with contextlib.suppress(httpx.HTTPError, ValueError):
        snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        for pending in snap.get("pending_elicitations") or []:
            if eid := pending.get("elicitation_id"):
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/elicitations/{eid}/resolve",
                    json={"action": "decline"},
                    timeout=10.0,
                )


def _park_permission_prompt(page: Page, base_url: str, session_id: str, mock_url: str) -> None:
    go_token = f"go-{uuid.uuid4().hex[:8]}"
    _script_write_prompt(mock_url, go_token)
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    _send(page, f"create the probe file now {go_token}")
    expect(page.locator(_PENDING_CARD).first).to_be_visible(timeout=120_000)
    # Show the terminal Claude is parked in, then return to the chat controls.
    _open_terminal_view(page)
    page.wait_for_timeout(2_000)
    _ensure_chat_view(page)


def _open_config_submenu(page: Page) -> None:
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=30_000)
    gear.click()
    edit_row = page.get_by_test_id("composer-agent-edit")
    expect(edit_row).to_be_visible(timeout=15_000)
    edit_row.click()


def _visible_notices(page: Page) -> list[str]:
    texts: list[str] = []
    for pill in page.locator(_ERROR_PILL).all():
        with contextlib.suppress(PlaywrightError):
            if pill.locator('[data-testid="error-message-content"]').count() == 0:
                pill.locator('[data-testid="error-headline"]').click()
            texts.append(pill.inner_text(timeout=2_000))
    for toast in page.locator(_TOAST).all():
        with contextlib.suppress(PlaywrightError):
            texts.append(toast.inner_text(timeout=2_000))
    return [re.sub(r"\s+", " ", text).strip() for text in texts if text.strip()]


def _wait_for_notice(page: Page) -> list[str]:
    deadline = time.monotonic() + _NOTICE_TIMEOUT_MS / 1000
    notices: list[str] = []
    while time.monotonic() < deadline:
        notices = _visible_notices(page)
        if notices:
            # Let the notice settle (the pill body renders after the expand click).
            page.wait_for_timeout(1_500)
            settled = _visible_notices(page)
            # Keep the first-seen notices in case a transient toast auto-dismissed.
            return list(dict.fromkeys(notices + settled))
        page.wait_for_timeout(500)
    return notices


def _assert_notice_names_the_prompt(notices: list[str], control: str) -> None:
    joined = " | ".join(notices)
    assert notices, (
        f"{control}: no error notice appeared within {_NOTICE_TIMEOUT_MS // 1000}s even though "
        "the terminal refused the change because a permission prompt is pending"
    )
    assert _GENERIC_RUNNER_FAILURE not in joined, (
        f"{control}: the notice only reports a generic runner failure instead of the pending "
        f"prompt: {joined!r}"
    )
    assert _NAMES_THE_PROMPT.search(joined), (
        f"{control}: the notice does not tell the person to answer the pending prompt: {joined!r}"
    )
    assert _NAMES_THE_NEXT_STEP.search(joined), (
        f"{control}: the notice names the blocker but omits the recovery step "
        f"(clear it in the terminal, then try again): {joined!r}"
    )


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_model_switch_blocked_by_pending_prompt_names_the_prompt(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    request.addfinalizer(lambda: _decline_pending(base_url, session_id))
    page: Page = request.getfixturevalue("page")
    _park_permission_prompt(page, base_url, session_id, mock_llm_server_url)

    # The gear's model picker issues this exact PATCH; the mock catalog has no
    # second model to click, so drive the switch directly and watch the open
    # page render the server's refusal notice.
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"model_override": "opus"},
        timeout=30.0,
    )
    assert response.status_code == 200, response.text

    notices = _wait_for_notice(page)
    print(f"model switch notices: {notices!r}")
    expect(page.locator(_PENDING_CARD).first).to_be_visible()
    _assert_notice_names_the_prompt(notices, "model switch")


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_effort_switch_blocked_by_pending_prompt_names_the_prompt(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    request.addfinalizer(lambda: _decline_pending(base_url, session_id))
    page: Page = request.getfixturevalue("page")
    _park_permission_prompt(page, base_url, session_id, mock_llm_server_url)

    _open_config_submenu(page)
    high = page.get_by_test_id("composer-agent-effort-high")
    expect(high).to_be_visible(timeout=15_000)
    expect(high).to_have_attribute("aria-checked", "false")
    high.click()

    notices = _wait_for_notice(page)
    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    print(
        f"effort switch notices: {notices!r}; persisted reasoning_effort="
        f"{snapshot.get('reasoning_effort')!r}"
    )
    expect(page.locator(_PENDING_CARD).first).to_be_visible()
    _assert_notice_names_the_prompt(notices, "effort switch")
