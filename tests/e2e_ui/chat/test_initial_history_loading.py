"""E2E coverage for what a session open is allowed to fetch.

Opening a session used to render one small page and then keep paging older
history from the transcript's layout effect until it found the previous user
prompt. The reader watched history land for seconds after the page had already
settled, with the content shifting under them, having never scrolled.

So: the open is exactly one items request, and nothing more happens until the
reader actually scrolls toward the top — at which point paging still works.
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, expect


def _seed_message(
    client: httpx.Client,
    session_id: str,
    *,
    response_id: str,
    role: str,
    text: str,
) -> None:
    """Persist one user or assistant message through the native event route."""
    content_type = "input_text" if role == "user" else "output_text"
    item_data: dict[str, object] = {
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }
    if role == "assistant":
        item_data["agent"] = "e2e-history"

    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": item_data,
                "response_id": response_id,
            },
        },
    )
    assert response.status_code == 202, response.text


def _track_items_requests(page: Page, session_id: str) -> None:
    """Record every `/items` request the page makes, in order."""
    endpoint = f"/v1/sessions/{session_id}/items"
    page.add_init_script(
        f"""
        (() => {{
          const endpoint = {json.dumps(endpoint)};
          window.__itemsUrls = [];
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {{
            const url = typeof input === "string" ? input : input.url;
            if (url.includes(endpoint)) window.__itemsUrls.push(url);
            return originalFetch(input, init);
          }};
        }})();
        """
    )


def _seed_long_transcript(base_url: str, session_id: str, latest_prompt: str) -> None:
    """Seed more items than one window holds, so older history remains."""
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        _seed_message(
            client, session_id, response_id="resp_old", role="user", text="oldest prompt"
        )
        # Comfortably beyond the initial window, so `has_more` stays true and
        # a regression that resumes paging has something to page into.
        for index in range(130):
            _seed_message(
                client,
                session_id,
                response_id=f"resp_fill_{index:03d}",
                role="assistant",
                text=f"filler {index}",
            )
        _seed_message(
            client, session_id, response_id="resp_latest", role="user", text=latest_prompt
        )


def test_opening_a_session_fetches_history_once_and_then_stops(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Open a session and fetch history exactly once, with no scrolling."""
    base_url, session_id = seeded_session
    latest_prompt = "history latest prompt"
    _seed_long_transcript(base_url, session_id, latest_prompt)
    _track_items_requests(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    conversation = page.get_by_role("log")
    expect(conversation.get_by_text(latest_prompt, exact=True)).to_be_visible(timeout=20_000)

    # Sit still, as a reader who has not scrolled. Background paging showed up
    # here as extra requests seconds after the transcript had settled.
    page.wait_for_timeout(4_000)

    urls = page.evaluate("window.__itemsUrls")
    assert len(urls) == 1, urls
    # One window-sized request, cursorless (the newest items).
    assert "after=" not in urls[0], urls
    # The "Loading earlier messages…" row belongs to reader-driven paging, so
    # it must never have appeared during the open either.
    expect(page.get_by_text("Loading earlier messages", exact=False)).to_have_count(0)


def test_scrolling_to_the_top_still_pages_older_history(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keep scroll-up paging working; only the automatic build is gone."""
    base_url, session_id = seeded_session
    latest_prompt = "history latest prompt"
    _seed_long_transcript(base_url, session_id, latest_prompt)
    _track_items_requests(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    conversation = page.get_by_role("log")
    expect(conversation.get_by_text(latest_prompt, exact=True)).to_be_visible(timeout=20_000)
    page.wait_for_timeout(2_000)
    assert len(page.evaluate("window.__itemsUrls")) == 1

    # Now the reader scrolls up to the top, which IS a request for older
    # history. (A pane with no scroll range at all can't report that movement;
    # that path arms on the wheel gesture itself and is unit-tested.)
    page.evaluate("document.querySelector('[role=\"log\"]').firstElementChild.scrollTop = 0")
    page.wait_for_function("window.__itemsUrls.length > 1", timeout=20_000)

    urls = page.evaluate("window.__itemsUrls")
    assert len(urls) >= 2, urls
    # Every page after the first is cursored off the window's oldest item.
    assert all("after=" in url for url in urls[1:]), urls
