"""A failed turn must surface its semantic failure description, not a generic pill.

Turn-failure normalization published the exception's diagnostic ``type`` (or a
``runner_error`` fallback) as the wire ``error.code``, discarding the explicit
semantic ``code``, and an inner-executor failure was tagged with its raw
exception class. Neither code exists in the SPA's ``FAILURE_CODE_DESCRIPTIONS``
map, so the failed-turn error pill read the generic "Something went wrong"
live and misattributed the failure to host setup after reload.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_PILL = '[data-testid="error-pill"]'
_WORKING = '[data-testid="working-indicator"]'
# ErrorBanner's headline for a failure code missing from
# FAILURE_CODE_DESCRIPTIONS ("Something went wrong") and runner_error's
# description ("Something went wrong setting up the turn on the host.") both
# start with this, so one filter catches the unmapped-code fallback and the
# host-setup misattribution.
_GENERIC_HEADLINE = "Something went wrong"
_EXECUTOR_ERROR_DESCRIPTION = "The agent runtime hit an error while running the turn."


def _send(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_inner_executor_failure_shows_executor_error_description(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    token = "provider-failure-executor"
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": "synthetic provider failure", "status_code": 400}],
        key="provider-failure-executor",
        match=token,
    )

    page.goto(f"{base_url}/c/{session_id}")
    _send(page, f"trigger a provider failure please {token}")

    pills = page.locator(_PILL)
    expect(pills.first).to_be_visible(timeout=90_000)
    # The failed status edge (which carries the normalized code) ends the turn,
    # so only a settled turn has every pill it will ever show.
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    expect(pills.filter(has_text=_EXECUTOR_ERROR_DESCRIPTION)).not_to_have_count(0)
    expect(pills.filter(has_text=_GENERIC_HEADLINE)).to_have_count(0)

    # The persisted failure (session-status last_task_error) rehydrates on
    # reload as a single deterministic pill; it must read the same semantic
    # description rather than the generic host-setup fallback.
    page.reload()
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=15_000)
    reloaded = page.locator(_PILL)
    expect(reloaded.first).to_be_visible(timeout=30_000)
    expect(reloaded.filter(has_text=_EXECUTOR_ERROR_DESCRIPTION)).not_to_have_count(0)
    expect(reloaded.filter(has_text=_GENERIC_HEADLINE)).to_have_count(0)
