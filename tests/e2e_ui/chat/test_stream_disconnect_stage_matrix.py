"""Numbered native output survives browser-open, shutdown, and retry windows."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Response, Route, expect

from tests.e2e_ui.conftest import _server_state, fetch_with_retry

_TOTAL = 1000
_INTERVAL_S = 0.01
_TIMEOUT_MS = 30_000
_MESSAGE_ID = "numbered-restart"


def _wait_for_stream(page: Page, epochs: list[str], count: int) -> str:
    deadline = time.monotonic() + _TIMEOUT_MS / 1000
    while len(epochs) < count and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert len(epochs) >= count, f"stream did not open {count} time(s): {epochs!r}"
    return epochs[count - 1]


def _numbered_delta(client: httpx.Client, session_id: str, number: int) -> None:
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_output_text_delta",
            "data": {
                "message_id": _MESSAGE_ID,
                "index": number - 1,
                "final": number == _TOTAL,
                "delta": f"{number} ",
            },
        },
    )
    response.raise_for_status()


@pytest.mark.timeout(300)
def test_numbered_output_recovers_across_stream_stages(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """One thousand numbers finish once despite setup, restart and failed reopen."""
    base_url, session_id = seeded_session
    restart = _server_state.get("restart_server")
    if not callable(restart):
        pytest.skip("requires the locally spawned restartable server")
    restart_server = cast("Callable[[], None]", restart)

    # Output produced before the browser subscribes comes from the server's
    # in-flight snapshot on initial open, not from a live SSE backlog.
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        for number in range(1, 31):
            _numbered_delta(client, session_id, number)

    epochs: list[str] = []
    failed_initial_opens = 0
    failed_reopens = 0
    opening_initial_stream = True
    midpoint = threading.Event()
    resume = threading.Event()
    tail_reached = threading.Event()
    finish = threading.Event()
    stop = threading.Event()
    output = "".join(f"{number} " for number in range(1, _TOTAL + 1))

    def native_session_snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != f"/v1/sessions/{session_id}" or (
            route.request.method != "GET"
        ):
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        route.fulfill(status=response.status, headers=response.headers, body=json.dumps(payload))

    def observe_stream(response: Response) -> None:
        if urlparse(response.url).path != f"/v1/sessions/{session_id}/stream":
            return
        if response.status == 200 and (epoch := response.headers.get("x-omnigent-stream-epoch")):
            epochs.append(epoch)

    def transient_stream_failure(route: Route) -> None:
        nonlocal failed_initial_opens, failed_reopens
        if urlparse(route.request.url).path != f"/v1/sessions/{session_id}/stream":
            route.continue_()
            return
        if opening_initial_stream and failed_initial_opens < 1:
            failed_initial_opens += 1
            route.fulfill(status=503, body="stream route not ready")
        elif not opening_initial_stream and failed_reopens < 2:
            failed_reopens += 1
            route.fulfill(status=503, body="server is restarting")
        else:
            route.continue_()

    def produce() -> None:
        with httpx.Client(base_url=base_url, timeout=3.0) as client:
            for number in range(31, _TOTAL + 1):
                if stop.is_set():
                    return
                try:
                    _numbered_delta(client, session_id, number)
                except httpx.HTTPError:
                    # Preview is best-effort only while the server is down.
                    if not 401 <= number <= 600:
                        raise
                if number == 400:
                    midpoint.set()
                if number == 600 and not resume.wait(timeout=90):
                    raise TimeoutError("browser did not reconnect after server restart")
                if number == 900:
                    tail_reached.set()
                    if not finish.wait(timeout=90):
                        raise TimeoutError("browser did not see the resumed stream")
                time.sleep(_INTERVAL_S)
        with httpx.Client(base_url=base_url, timeout=20.0) as client:
            result = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "external_conversation_item",
                    "data": {
                        "source_id": "numbered-final-1-1000",
                        "item_type": "message",
                        "response_id": "numbered-response",
                        "item_data": {
                            "role": "assistant",
                            "agent": "claude-native-ui",
                            "content": [{"type": "output_text", "text": output}],
                        },
                    },
                },
            )
            result.raise_for_status()

    page.route("**/v1/sessions/**", native_session_snapshot)
    page.route("**/v1/sessions/**/stream*", transient_stream_failure)
    page.on("response", observe_stream)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=_TIMEOUT_MS)
        first_epoch = _wait_for_stream(page, epochs, 1)
        assert failed_initial_opens == 1
        opening_initial_stream = False
        section = page.locator('[data-testid="assistant-text-section"]', has_text="1 2 3 ")
        expect(section).to_contain_text("29 30 ", timeout=_TIMEOUT_MS)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(produce)
            try:
                assert midpoint.wait(timeout=30), "numbered producer did not reach 400"
                expect(section).to_contain_text("399 400 ", timeout=_TIMEOUT_MS)
                # The server restart drops the active socket; the next two
                # browser stream opens fail before a healthy subscription.
                restart_server()
                assert _wait_for_stream(page, epochs, 2) != first_epoch
                assert failed_reopens == 2
                expect(section).to_be_visible(timeout=_TIMEOUT_MS)
                expect(page.get_by_test_id("stream-interruption-notice")).to_be_visible(
                    timeout=_TIMEOUT_MS
                )
                resume.set()
                assert tail_reached.wait(timeout=60), "numbered producer did not reach 900"
                expect(section).to_contain_text("899 900 ", timeout=_TIMEOUT_MS)
                # Simulate a half-open browser socket: the visibility wake
                # recycles a byte-stale stream on the same server process.
                page.evaluate(
                    """() => {
                      const realNow = Date.now;
                      Date.now = () => realNow() + 36_000;
                      document.dispatchEvent(new Event('visibilitychange'));
                      Date.now = realNow;
                    }"""
                )
                assert _wait_for_stream(page, epochs, 3) == epochs[1]
                expect(section).to_contain_text("899 900 ", timeout=_TIMEOUT_MS)
                finish.set()
                future.result(timeout=90)
            finally:
                stop.set()
                resume.set()
                finish.set()

        committed = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=20.0)
        committed.raise_for_status()
        stored_texts = [
            block["text"]
            for item in committed.json()["data"]
            if item.get("type") == "message" and item.get("role") == "assistant"
            for block in item.get("content", [])
            if block.get("type") == "output_text"
        ]
        assert stored_texts.count(output) == 1
        expect(
            page.locator('[data-testid="assistant-text-section"]', has_text=output)
        ).to_have_count(1, timeout=_TIMEOUT_MS)
        expect(page.get_by_test_id("stream-interruption-notice")).to_have_count(0)

        page.reload()
        expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=_TIMEOUT_MS)
        expect(
            page.locator('[data-testid="assistant-text-section"]', has_text="899 900 ")
        ).to_have_count(1, timeout=_TIMEOUT_MS)
        expect(page.get_by_test_id("stream-interruption-notice")).to_have_count(0)
    finally:
        page.remove_listener("response", observe_stream)
