"""Browser-lane coverage for a session whose snapshot fails to load."""

from __future__ import annotations

from playwright.sync_api import Page, Route, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_QUESTION = "An earlier question?"


def _fail_snapshots(chat: ChatSessionContract, times: int) -> dict[str, int]:
    """Fail the session's next `times` snapshot GETs; set `times` on the result to change it."""
    left = {"times": times}
    session_url = f"{chat.contract.base_url}/v1/sessions/{chat.session_id}"

    def answer(route: Route) -> None:
        if route.request.method == "GET" and left["times"] > 0:
            left["times"] -= 1
            route.fulfill(status=503, json={"error": {"message": "server restarting"}})
            return
        route.fallback()

    chat.contract.route(session_url, answer)
    chat.contract.route(f"{session_url}?*", answer)
    return left


def test_a_session_that_keeps_failing_to_load_can_be_loaded_again(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Once the retries run out, the error says the load failed and offers to try again."""
    chat = chat_session_contract
    chat.set_items([message_item("load-user", "user", _QUESTION, response_id="r1")])
    failing = _fail_snapshots(chat, 1_000)

    page.goto(chat.url)
    heading = page.get_by_role("heading", name="Couldn't load this conversation")
    expect(heading).to_be_visible(timeout=20_000)
    failing["times"] = 0
    page.get_by_role("button", name="Try again").click()

    expect(page.get_by_text(_QUESTION)).to_be_visible(timeout=10_000)
    expect(heading).to_have_count(0)
