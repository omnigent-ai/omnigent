"""A refused model connection on the openai-agents harness keeps its connection_error code.

The OpenAI client raises ``APIConnectionError`` when the agent's base URL refuses
connections. The failed turn must then read as a connection error everywhere a
user or dashboard looks: the chat pill (live and after reload), the session's
``last_task_error`` and the persisted error item.
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

CONNECTION_ERROR_HEADLINE = (
    "The connection to the agent dropped mid-turn; retrying usually continues the turn."
)


def _unreachable_endpoint() -> tuple[socket.socket, str]:
    """An OpenAI-compatible base URL on a bound-but-not-listening loopback port.

    The socket is held open (never ``listen``) so connections stay refused and the
    port cannot be reassigned mid-test; the caller must close it when done.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    return sock, f"http://127.0.0.1:{port}/v1"


def _create_dead_endpoint_session(base_url: str, runner_id: str, endpoint: str) -> str:
    """Register an openai-agents agent pinned at *endpoint* and bind its session."""
    name = f"conn-error-{uuid.uuid4().hex[:8]}"
    spec = (
        f"name: {name}\n"
        "prompt: You are a terse assistant.\n"
        "executor:\n"
        "  harness: openai-agents\n"
        "  model: gpt-4o-mini\n"
        "  auth:\n"
        "    type: api_key\n"
        "    api_key: mock-key\n"
        f"    base_url: {endpoint}\n"
    ).encode()
    create = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        bundle_files({f"{name}.yaml": spec}),
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _error_items(base_url: str, session_id: str) -> list[dict[str, object]]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    items = body.get("data", body) if isinstance(body, dict) else body
    return [item for item in items if isinstance(item, dict) and item.get("type") == "error"]


def _expand_pill(page: Page, pill: Locator) -> tuple[str, str]:
    """Return the pill's headline and, after expanding it, its raw message."""
    headline = pill.get_by_test_id("error-headline")
    expect(headline).not_to_be_empty()
    pill.locator('button[aria-expanded="false"]').first.click()
    message = pill.get_by_test_id("error-message-content")
    expect(message).not_to_be_empty()
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR") or os.environ.get("E2E_SCREENSHOT_DIR"):
        # Hold the expanded pill so a recording of the journey stays readable.
        page.wait_for_timeout(1_500)
    return headline.inner_text(), message.inner_text()


def _screenshot(page: Page, name: str) -> None:
    if screenshot_dir := os.environ.get("E2E_SCREENSHOT_DIR"):
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / f"{name}.png"))


@pytest.mark.timeout(300)
def test_refused_model_connection_keeps_connection_error_code(
    request: pytest.FixtureRequest,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    endpoint_socket, endpoint = _unreachable_endpoint()
    session_id: str | None = None
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_dead_endpoint_session(live_server, runner_id, endpoint)
        page: Page = request.getfixturevalue("page")
        try:
            page.goto(f"{live_server}/c/{session_id}")
            composer = page.get_by_role("textbox", name="Message the agent")
            expect(composer).to_be_visible(timeout=15_000)
            composer.fill("hello")
            page.get_by_role("button", name="Send", exact=True).click()

            # The OpenAI client retries the refused connection with backoff before giving up.
            pill = page.get_by_test_id("error-pill").first
            expect(pill).to_be_visible(timeout=120_000)
            expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)
            live_headline, live_message = _expand_pill(page, pill)
            _screenshot(page, "live-pill")

            snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            snapshot.raise_for_status()
            last_error = snapshot.json()["last_task_error"]
            items = _error_items(live_server, session_id)

            page.reload()
            expect(pill).to_be_visible(timeout=15_000)
            reloaded_headline, reloaded_message = _expand_pill(page, pill)
            _screenshot(page, "reloaded-pill")
        finally:
            # Finalize the recording on the pill, before the session is deleted.
            page.context.close()

        observed = (
            f"live headline={live_headline!r}, live message={live_message!r}, "
            f"reloaded headline={reloaded_headline!r}, reloaded message={reloaded_message!r}, "
            f"last_task_error={last_error!r}, error items={items!r}"
        )
        assert live_headline == CONNECTION_ERROR_HEADLINE, observed
        assert live_message == "Connection error.", observed
        assert reloaded_headline == CONNECTION_ERROR_HEADLINE, observed
        assert reloaded_message == "Connection error.", observed
        assert (last_error or {}).get("code") == "connection_error", observed
        assert (last_error or {}).get("message") == "Connection error.", observed
        assert [(item.get("code"), item.get("message")) for item in items] == [
            ("connection_error", "Connection error.")
        ], observed
    finally:
        endpoint_socket.close()
        try:
            if session_id is not None:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except Exception:
                    respawned.kill()
                    respawned.wait(timeout=5)
