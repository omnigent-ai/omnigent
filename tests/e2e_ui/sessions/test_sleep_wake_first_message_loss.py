"""Browser regression for an interrupted first send across a hard reload.

The initial /events POST is aborted, then a reload permits recovery against a
real seeded session. Host, agent, and create responses are stubbed because the
headless harness cannot launch a runner.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

_PROMPT = "sentinel first message that must survive sleep and wake"

_EVENTS_RE = re.compile(r"/v1/sessions/([^/]+)/events$")
# Match the collection endpoint; session reads still hit the server.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run an async browser journey in a fresh thread to avoid pytest loop conflicts."""
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    """Poll until the predicate succeeds or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def test_sleep_wake_does_not_lose_first_message(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """An interrupted first send survives a hard reload and reaches the transcript."""
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_sleep_wake(base_url, session_a, session_b))


async def _drive_sleep_wake(base_url: str, session_a: str, session_b: str) -> None:
    """Exercise the composer-to-session handoff against a real seeded session."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Close the context first so an optional video recording is flushed.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            events_before: list[tuple[str, str]] = []
            events_after: list[tuple[str, str]] = []
            woke = {"v": False}

            async def handle_events(route: Route) -> None:
                request = route.request
                match = _EVENTS_RE.search(request.url)
                assert match is not None, f"unexpected /events url: {request.url}"
                body = request.post_data_json
                text = body["data"]["content"][0]["text"]
                sid = match.group(1)
                if not woke["v"]:
                    events_before.append((sid, text))
                    await route.abort("connectionaborted")
                else:
                    events_after.append((sid, text))
                    await route.continue_()

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": "host_e2e",
                                    "name": "e2e-host",
                                    "owner": "e2e",
                                    "status": "online",
                                }
                            ]
                        }
                    ),
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "data": [
                                {
                                    "id": "ag_e2e",
                                    "name": "hello_world",
                                    "display_name": "Hello World",
                                    "description": None,
                                    "harness": None,
                                }
                            ]
                        }
                    ),
                )

            async def handle_sessions(route: Route) -> None:
                # Only creation is stubbed; reads and the recovered send use the server.
                if route.request.method == "POST":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_a}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route(_SESSIONS_RE, handle_sessions)

            # Start in B so creating A exercises client-side navigation.
            await page.goto(f"{base_url}/c/{session_b}")

            # The stub host needs a recent working directory to enable Send.
            await page.evaluate(
                """() => localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({ host_e2e: ["/tmp"] }),
                )"""
            )

            await page.get_by_test_id("new-chat-button").click()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=15_000
            )
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            await page.get_by_test_id("new-chat-landing-submit").click()

            # Confirm the first send was attempted before the reload.
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))
            await _wait_until(
                lambda: any(sid == session_a and text == _PROMPT for sid, text in events_before)
            )

            # Reload removes the in-memory handoff while preserving sessionStorage.
            woke["v"] = True
            await page.reload()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))

            assert any(sid == session_a and text == _PROMPT for sid, text in events_before), (
                "the initial prompt never auto-sent to session A before the reload; "
                f"pre-wake /events posts were {events_before} — the test did not exercise "
                "the first-message handoff at all"
            )

            # A composer draft is not delivery; require a transcript bubble.
            await expect(
                page.get_by_test_id("message-bubble").filter(has_text=_PROMPT).first
            ).to_be_visible(timeout=20_000)
            # The post-reload send must also reach the server.
            await _wait_until(
                lambda: any(sid == session_a and text == _PROMPT for sid, text in events_after),
                timeout_s=15.0,
            )
        finally:
            await context.close()
            await browser.close()
