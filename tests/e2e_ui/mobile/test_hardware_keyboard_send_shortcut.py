"""E2E: a hardware keyboard's Cmd/Ctrl+Enter sends on a touch device, while plain
Enter stays a newline (desktop Chromium at a phone profile stands in)."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.conftest import configure_mock_llm

_PHONE = pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 664},
    device_scale_factor=3,
    is_mobile=True,
    has_touch=True,
    user_agent=(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/26.4 Mobile/15E148 Safari/604.1"
    ),
)

_COMPOSER_LABEL = "Message the agent"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_TOGGLE = "composer-submit-with-mod-enter-toggle"
_SEND_TIMEOUT_MS = 10_000
_TURN_TIMEOUT_MS = 30_000

# Equal-length routing tokens: on the resent transcript the mock picks the longest
# match, then the rightmost (newest turn), so each turn gets its own reply.
_META_TOKEN = "hardware-meta-enter"
_CTRL_TOKEN = "hardware-ctrl-enter"
assert len(_META_TOKEN) == len(_CTRL_TOKEN)
_META_PROMPT = f"{_META_TOKEN} summarize the deploy status"
_META_REPLY = "deploy-status-summary-reply"
_CTRL_PROMPT = f"{_CTRL_TOKEN} list the open incidents"
_CTRL_REPLY = "open-incident-list-reply"


def _record_message_posts(page: Page, session_id: str) -> list[str]:
    posts: list[str] = []

    def record(request: Request) -> None:
        if request.method != "POST":
            return
        if urlparse(request.url).path != f"/v1/sessions/{session_id}/events":
            return
        body = request.post_data_json
        if not isinstance(body, dict) or body.get("type") != "message":
            return
        for block in body.get("data", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "input_text":
                posts.append(str(block.get("text", "")))

    page.on("request", record)
    return posts


def _turn_on_mod_enter_setting(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings/general")
    # At a phone width the settings nav opens as an overlay; tapping General closes it.
    nav_item = page.get_by_test_id("settings-nav-general")
    expect(nav_item).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    nav_item.tap()
    toggle = page.get_by_test_id(_TOGGLE)
    expect(toggle).to_have_attribute("aria-checked", "false", timeout=_TURN_TIMEOUT_MS)
    toggle.tap()
    expect(toggle).to_have_attribute("aria-checked", "true")


def _open_composer(page: Page, base_url: str, session_id: str):
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    assert page.evaluate("matchMedia('(pointer: coarse)').matches") is True
    assert page.evaluate("matchMedia('(max-width: 767.98px)').matches") is True
    return composer


def _expect_sent(page: Page, prompt: str, reply: str) -> None:
    expect(page.locator(_USER_BUBBLE).filter(has_text=prompt)).to_be_visible(
        timeout=_SEND_TIMEOUT_MS
    )
    expect(page.locator(_ASSISTANT_BUBBLE).filter(has_text=reply)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )


@_PHONE
@pytest.mark.parametrize(
    "submit_with_mod_enter", [False, True], ids=["enter-sends", "mod-enter-sends"]
)
def test_hardware_mod_enter_sends_on_touch_device(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    submit_with_mod_enter: bool,
) -> None:
    base_url, session_id = seeded_session
    for prompt, reply, token in (
        (_META_PROMPT, _META_REPLY, _META_TOKEN),
        (_CTRL_PROMPT, _CTRL_REPLY, _CTRL_TOKEN),
    ):
        configure_mock_llm(mock_llm_server_url, [{"text": reply}] * 2, key=prompt, match=token)

    page: Page = request.getfixturevalue("page")
    posts = _record_message_posts(page, session_id)
    if submit_with_mod_enter:
        _turn_on_mod_enter_setting(page, base_url)
    composer = _open_composer(page, base_url, session_id)

    # The on-screen keyboard's Enter stays a newline on a touch device.
    composer.tap()
    composer.type(_META_PROMPT)
    composer.press("Enter")
    composer.type("second line")
    expect(composer).to_have_value(f"{_META_PROMPT}\nsecond line")
    page.wait_for_timeout(500)
    assert posts == []

    composer.press("Meta+Enter")
    _expect_sent(page, _META_PROMPT, _META_REPLY)
    assert posts == [f"{_META_PROMPT}\nsecond line"]
    expect(composer).to_have_value("")

    composer.tap()
    composer.type(_CTRL_PROMPT)
    composer.press("Control+Enter")
    _expect_sent(page, _CTRL_PROMPT, _CTRL_REPLY)
    assert posts == [f"{_META_PROMPT}\nsecond line", _CTRL_PROMPT]
