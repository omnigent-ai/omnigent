"""E2E: chat hyperlinks must still navigate where popup creation is blocked.

The Omnigent SPA is also mounted inside host applications (e.g. a workspace
browser pane) that do not grant popup creation, so ``window.open`` /
``target="_blank"`` is silently swallowed there. Chat links render with
``target="_blank"`` only, so in such a host a plain click does nothing: no
new tab, no navigation. The minimum acceptance criterion is that a click
performs a working navigation, with same-tab navigation as the required
fallback when opening a new tab is unavailable.

The popup-blocked host is modelled with a same-origin page embedding the
chat in an ``<iframe sandbox>`` that grants scripts/network/forms but not
``allow-popups`` — the standard way an embedder withholds popup creation.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

_AGENT_NAME = "hello_world"
_LINK_LABEL = "server health"

# Host-page sandbox grants: the embedded SPA fully works (scripts, network,
# cookies, forms) but the host does not grant popup creation, so
# `target="_blank"` / `window.open` cannot open a new tab or window.
_POPUP_BLOCKED_SANDBOX = "allow-scripts allow-same-origin allow-forms"


@pytest.fixture
def embedded_link_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a deterministic assistant reply containing an external link."""
    base_url, session_id = seeded_session
    link_url = f"{base_url}/health"
    event_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": _AGENT_NAME,
                "text": f"Check the [{_LINK_LABEL}]({link_url}).",
            },
        },
        timeout=10.0,
    )
    event_resp.raise_for_status()
    yield (base_url, session_id, link_url)


@pytest.mark.parametrize("activation", ["click", "keyboard"])
def test_chat_link_click_navigates_in_popup_blocked_host(
    page: Page,
    embedded_link_session: tuple[str, str, str],
    activation: str,
) -> None:
    """A clicked chat link navigates even when the host blocks popups.

    In a host that withholds popup creation, `target="_blank"` alone means a
    plain click does nothing. The required fallback is a working same-tab
    navigation: the embedded chat frame leaves the chat page for the link
    target. This test fails while the click is a silent no-op.
    """
    base_url, session_id, link_url = embedded_link_session
    chat_url = f"{base_url}/c/{session_id}"
    host_url = f"{base_url}/e2e-popup-blocked-host"

    host_html = f"""<!doctype html>
<html>
  <head><title>Popup-blocked host pane</title></head>
  <body style="margin:0">
    <iframe id="embed" sandbox="{_POPUP_BLOCKED_SANDBOX}" src="{chat_url}"
            style="width:100vw;height:100vh;border:0"></iframe>
  </body>
</html>"""

    def serve_host(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html", body=host_html)

    page.route(host_url, serve_host)
    page.goto(host_url)

    frame = page.frame_locator("#embed")
    link = frame.get_by_role("link", name=_LINK_LABEL)
    expect(link).to_be_visible(timeout=30_000)
    expect(link).to_have_attribute("href", link_url)

    with page.expect_request(link_url) as request_info:
        if activation == "keyboard":
            link.focus()
            link.press("Enter")
        else:
            link.click()

    expect(frame.locator("body")).to_contain_text('"status":"ok"', timeout=10_000)
    assert page.locator("#embed").evaluate("el => el.contentWindow.location.href") == link_url
    assert frame.locator("body").evaluate("el => el.ownerDocument.referrer") == ""
    assert "referer" not in request_info.value.all_headers()
    expect(page).to_have_url(host_url)
    assert len(page.context.pages) == 1
