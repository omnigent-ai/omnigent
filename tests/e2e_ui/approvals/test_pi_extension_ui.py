"""Browser-only boundaries: confirmation layout and multiline editor submission."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_FORM = '[data-testid="ask-user-question-form"]'
_SUBMIT = '[data-testid="ask-user-question-submit"]'

_MOCK_ELICITATION_TIMEOUT_MS = 15_000

_EDITOR_TITLE = "Edit note"
_CONFIRM_TITLE = "Confirm production deploy"
_CONFIRM_MESSAGE = "Really deploy v2.4.0 to production right now?"


@pytest.mark.timeout(90)
@pytest.mark.parametrize("method", ["confirm", "editor"])
def test_pi_extension_dialog_renders_and_returns_the_answer(
    page: Page,
    seeded_session: tuple[str, str],
    method: str,
) -> None:
    base_url, session_id = seeded_session

    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(
            httpx.post,
            f"{base_url}/v1/sessions/{session_id}/hooks/pi-extension-ui",
            json={
                "elicitation_id": "elicit_pi_" + "f" * 32,
                "request": {
                    "id": "uuid-select",
                    "method": method,
                    "title": _CONFIRM_TITLE if method == "confirm" else _EDITOR_TITLE,
                    **({"message": _CONFIRM_MESSAGE} if method == "confirm" else {}),
                    **({"prefill": "original note"} if method == "editor" else {}),
                },
            },
            timeout=60,
        )

        page.goto(f"{base_url}/c/{session_id}")

        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
        if method == "confirm":
            expect(card.locator(_FORM)).to_have_count(0)
            expect(card.get_by_text(_CONFIRM_TITLE, exact=False)).to_be_visible()
            message = card.get_by_text(_CONFIRM_MESSAGE, exact=False)
            expect(message).to_be_visible()
            expect(message).to_have_css("white-space", "pre-line")
            card.get_by_role("button", name="Approve").click()
        else:
            expect(card.get_by_text("Pi needs your input")).to_be_visible()
            form = card.locator(_FORM)
            expect(form.get_by_text(_EDITOR_TITLE, exact=True)).to_be_visible()
            expected = "two\nlines"
            form.get_by_role("textbox").fill(expected)
            form.locator(_SUBMIT).click()

        expect(page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first).to_be_visible(
            timeout=_MOCK_ELICITATION_TIMEOUT_MS
        )

        response = pending.result(timeout=30)
        response.raise_for_status()
        verdict = response.json()
        assert verdict["type"] == "extension_ui_response", verdict
        if method == "confirm":
            assert verdict["confirmed"] is True, verdict
        else:
            assert verdict["value"] == expected, verdict
