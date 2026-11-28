"""Keep native preview prefixes across a real server restart under SSE load."""

from __future__ import annotations

import contextlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Response, Route, expect

from tests.e2e_ui.conftest import _create_runner_bound_session, _server_state, fetch_with_retry

_STREAMS = 4
_CHUNKS_PER_PHASE = 32
_TIMEOUT_MS = 90_000
_RECONNECT_TIMEOUT_MS = 20_000


def _chunk(message: int, phase: str, index: int) -> str:
    return f"pressure-{message}-{phase}-{index:03d}|"


def _send_chunks(base_url: str, session_ids: list[str], phase: str) -> None:
    """Post ordered chunks per session, across four sessions in parallel."""

    def send_message(message: int) -> None:
        offset = 0 if phase == "before" else _CHUNKS_PER_PHASE
        with httpx.Client(base_url=base_url, timeout=20.0) as client:
            for index in range(_CHUNKS_PER_PHASE):
                response = client.post(
                    f"/v1/sessions/{session_ids[message]}/events",
                    json={
                        "type": "external_output_text_delta",
                        "data": {
                            "message_id": f"pressure-{message}",
                            "index": offset + index,
                            "final": phase == "after" and index == _CHUNKS_PER_PHASE - 1,
                            "delta": _chunk(message, phase, index),
                        },
                    },
                )
                response.raise_for_status()

    with ThreadPoolExecutor(max_workers=_STREAMS) as pool:
        list(pool.map(send_message, range(_STREAMS)))


def _wait_for_stream_epoch(
    page: Page, epochs: list[str], count: int, *, timeout_ms: int = _TIMEOUT_MS
) -> str:
    deadline = time.monotonic() + timeout_ms / 1000
    while len(epochs) < count and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert len(epochs) >= count, f"only {len(epochs)} session stream(s) opened"
    return epochs[count - 1]


@pytest.mark.timeout(300)
def test_native_stream_prefix_survives_server_restart_under_load(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """One visible native message survives while four sessions stream concurrently."""
    base_url, session_id = seeded_session
    restart = _server_state.get("restart_server")
    if not callable(restart):
        pytest.skip("requires the locally spawned restartable server")
    restart_server = restart
    previous_pid = int(cast(int, _server_state["pid"]))
    background_sessions = [
        _create_runner_bound_session(base_url, str(_server_state["runner_id"]))
        for _ in range(_STREAMS - 1)
    ]
    session_ids = [session_id, *background_sessions]
    epochs: list[str] = []

    def observe_stream(response: Response) -> None:
        if urlparse(response.url).path != f"/v1/sessions/{session_id}/stream":
            return
        if response.status != 200:
            return
        epoch = response.headers.get("x-omnigent-stream-epoch")
        if epoch:
            epochs.append(epoch)

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

    # The real server receives native-shaped events; the browser must also
    # recognize the seeded session as native to exercise preview finalization.
    page.route("**/v1/sessions/**", native_session_snapshot)
    page.on("response", observe_stream)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)
        first_epoch = _wait_for_stream_epoch(page, epochs, 1)

        _send_chunks(base_url, session_ids, "before")
        section = page.locator(
            '[data-testid="assistant-text-section"]', has_text=_chunk(0, "before", 0)
        )
        expect(section).to_contain_text(
            _chunk(0, "before", _CHUNKS_PER_PHASE - 1), timeout=_TIMEOUT_MS
        )

        restart_server()
        assert int(cast(int, _server_state["pid"])) != previous_pid
        assert (
            _wait_for_stream_epoch(page, epochs, 2, timeout_ms=_RECONNECT_TIMEOUT_MS)
            != first_epoch
        )
        expect(section).to_be_visible(timeout=_RECONNECT_TIMEOUT_MS)

        _send_chunks(base_url, session_ids, "after")
        expect(section).to_contain_text(
            _chunk(0, "after", _CHUNKS_PER_PHASE - 1), timeout=_TIMEOUT_MS
        )
        full_text = "".join(
            _chunk(0, phase, index)
            for phase in ("before", "after")
            for index in range(_CHUNKS_PER_PHASE)
        )
        result = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "source_id": "pressure-final-0",
                    "item_type": "message",
                    "response_id": "pressure-response-0",
                    "item_data": {
                        "role": "assistant",
                        "agent": "claude-native-ui",
                        "stream_message_id": "pressure-0",
                        "content": [{"type": "output_text", "text": full_text}],
                    },
                },
            },
            timeout=20.0,
        )
        result.raise_for_status()
        expect(
            page.locator('[data-testid="assistant-text-section"]', has_text=full_text)
        ).to_have_count(1, timeout=_TIMEOUT_MS)
    finally:
        page.remove_listener("response", observe_stream)
        for background_id in background_sessions:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{background_id}", timeout=10.0)
