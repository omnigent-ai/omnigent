"""E2E coverage for non-blocking initial conversation history loading.

The transcript is seeded so its newest page contains only the latest user
prompt. The previous prompt is on page two, with still-older history beyond
it. The browser records whether the latest prompt is already rendered when
each items request starts, proving the second request is post-paint work.

A second case seeds a turn long enough that the window needs four pages, and
pins that flow: every page is fetched, paging stops at the prompt boundary,
and both prompts end up on screen.
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


def test_initial_history_renders_before_prompt_boundary_fetch_finishes(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Render page one before fetching page two and keep the turn rail lazy."""
    base_url, session_id = seeded_session
    previous_prompt = "history previous prompt"
    latest_prompt = "history latest prompt"

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        # Twenty old items leave more history after page two. The next 21 items
        # place one user prompt on each of the two newest 20-item pages.
        for index in range(20):
            _seed_message(
                client,
                session_id,
                response_id=f"resp_old_{index:02d}",
                role="assistant",
                text=f"old filler {index}",
            )
        _seed_message(
            client,
            session_id,
            response_id="resp_previous",
            role="user",
            text=previous_prompt,
        )
        _seed_message(
            client,
            session_id,
            response_id="resp_previous",
            role="assistant",
            text="previous reply",
        )
        _seed_message(
            client,
            session_id,
            response_id="resp_latest",
            role="user",
            text=latest_prompt,
        )
        for index in range(18):
            _seed_message(
                client,
                session_id,
                response_id=f"resp_latest_{index:02d}",
                role="assistant",
                text=f"latest filler {index}",
            )

    endpoint = f"/v1/sessions/{session_id}/items"
    page.add_init_script(
        f"""
        (() => {{
          const endpoint = {json.dumps(endpoint)};
          const latestPrompt = {json.dumps(latest_prompt)};
          const originalFetch = window.fetch.bind(window);
          window.__historyFetches = [];
          window.fetch = (input, init) => {{
            const url = typeof input === "string" ? input : input.url;
            if (url.includes(endpoint)) {{
              window.__historyFetches.push({{
                url,
                latestVisible: document.body.innerText.includes(latestPrompt),
              }});
            }}
            return originalFetch(input, init);
          }};
        }})();
        """
    )

    page.set_viewport_size({"width": 1280, "height": 320})
    page.goto(f"{base_url}/c/{session_id}")

    conversation = page.get_by_role("log")
    expect(conversation.get_by_text(latest_prompt, exact=True)).to_be_visible(timeout=15_000)
    expect(conversation.get_by_text(previous_prompt, exact=True)).to_be_visible(timeout=15_000)
    page.wait_for_function("window.__historyFetches.length >= 2", timeout=15_000)
    page.wait_for_timeout(500)

    fetches = page.evaluate("window.__historyFetches")
    assert len(fetches) == 2, fetches
    assert fetches[0]["latestVisible"] is False
    assert fetches[1]["latestVisible"] is True
    assert all("limit=20" in request["url"] for request in fetches)
    assert all("limit=200" not in request["url"] for request in fetches)

    expect(page.get_by_role("button", name=f"Jump to: {previous_prompt}")).to_be_visible()
    expect(page.get_by_role("button", name=f"Jump to: {latest_prompt}")).to_be_visible()


def test_multi_page_initial_window_reaches_the_prompt_boundary(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Grow a window that needs several pages, without stalling short of it.

    The newest three pages are pure tool-style filler, so reaching the prompt
    boundary takes four requests. The store now gathers those pages itself and
    prepends them once, instead of the transcript paging one per render; this
    pins the resulting flow — every page is still fetched, the window still
    stops at the boundary, and both prompts end up on screen.

    The one-commit property itself is asserted in chatStore.test.ts, which can
    count store commits directly; locally the pages return fast enough that
    both the old and new paths coalesce in the DOM, so it is not observable
    from here.
    """
    base_url, session_id = seeded_session
    previous_prompt = "reflow previous prompt"
    latest_prompt = "reflow latest prompt"

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        _seed_message(
            client, session_id, response_id="resp_prev", role="user", text=previous_prompt
        )
        _seed_message(
            client, session_id, response_id="resp_prev", role="assistant", text="previous reply"
        )
        _seed_message(
            client, session_id, response_id="resp_latest", role="user", text=latest_prompt
        )
        # 61 filler items after the latest prompt: the newest three 20-item
        # pages hold no prompt at all, so the window needs a fourth request.
        for index in range(61):
            _seed_message(
                client,
                session_id,
                response_id=f"resp_fill_{index:02d}",
                role="assistant",
                text=f"filler {index}",
            )

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

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    conversation = page.get_by_role("log")
    # The window reached back past the long turn to the preceding prompt.
    expect(conversation.get_by_text(previous_prompt, exact=True)).to_be_visible(timeout=20_000)
    expect(conversation.get_by_text(latest_prompt, exact=True)).to_be_visible(timeout=20_000)
    page.wait_for_timeout(1_000)

    urls = page.evaluate("window.__itemsUrls")
    # Four 20-item pages: three of filler, then the one holding both prompts.
    # It must stop there rather than keep paging to the start of the session.
    assert len(urls) == 4, urls
    assert all("limit=20" in url for url in urls)
    assert sum(1 for url in urls if "after=" in url) == 3, urls

    expect(page.get_by_role("button", name=f"Jump to: {previous_prompt}")).to_be_visible()
    expect(page.get_by_role("button", name=f"Jump to: {latest_prompt}")).to_be_visible()
