"""A refused model connection on the openai-agents harness keeps its connection_error code."""

from __future__ import annotations

import io
import json
import os
import re
import socket
import tarfile
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

_GENERIC_HEADLINE = re.compile(
    r"^(?:Something went wrong(?: setting up the turn on the host\.)?"
    r"|.+ ran into an error during this turn\.)$"
)


def _unreachable_endpoint() -> str:
    """Return an OpenAI-compatible base URL on a local port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def _create_openai_agents_session(base_url: str, runner_id: str, endpoint: str) -> str:
    """Register an openai-agents agent pinned at *endpoint* and bind its session."""
    name = f"conn-error-{uuid.uuid4().hex[:8]}"
    config = {
        "name": name,
        "prompt": "You are a terse assistant.",
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o-mini",
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": endpoint},
        },
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.safe_dump(config, sort_keys=False).encode()
        info = tarfile.TarInfo(f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    bind = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id}, timeout=10.0
    )
    bind.raise_for_status()
    return session_id


def _error_item_codes(base_url: str, session_id: str) -> list[object]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    items = body.get("data", body) if isinstance(body, dict) else body
    return [
        item.get("code")
        for item in items
        if isinstance(item, dict) and item.get("type") == "error"
    ]


def _expand_pill(pill: Locator) -> tuple[str, str]:
    """Return the pill's headline and, after expanding it, its raw message."""
    headline = pill.get_by_test_id("error-headline")
    expect(headline).not_to_be_empty()
    pill.locator('button[aria-expanded="false"]').first.click()
    message = pill.get_by_test_id("error-message-content")
    expect(message).to_contain_text("Connection error")
    return headline.inner_text(), message.inner_text()


@pytest.mark.timeout(300)
def test_model_connection_error_keeps_its_classification(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A refused model connection surfaces as a connection error, not a generic failure."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_openai_agents_session(live_server, runner_id, _unreachable_endpoint())
        try:
            page.goto(f"{live_server}/c/{session_id}")
            composer = page.get_by_role("textbox", name="Message the agent")
            expect(composer).to_be_visible(timeout=15_000)
            composer.fill("hello")
            page.get_by_role("button", name="Send", exact=True).click()

            # The OpenAI client retries the refused connection with backoff before giving up.
            pill = page.get_by_test_id("error-pill").first
            expect(pill).to_be_visible(timeout=120_000)
            expect(page.locator('[data-testid="working-indicator"]')).to_have_count(
                0, timeout=60_000
            )
            live_headline, live_message = _expand_pill(pill)
            if screenshot_dir := os.environ.get("E2E_SCREENSHOT_DIR"):
                Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(screenshot_dir) / "model-connection-error-pill.png"))

            snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            snapshot.raise_for_status()
            last_error = snapshot.json()["last_task_error"]
            item_codes = _error_item_codes(live_server, session_id)

            page.reload()
            expect(pill).to_be_visible(timeout=15_000)
            reloaded_headline, _ = _expand_pill(pill)

            observed = (
                f"live headline={live_headline!r}, reloaded headline={reloaded_headline!r}, "
                f"message={live_message!r}, last_task_error={last_error!r}, "
                f"error item codes={item_codes!r}"
            )
            assert not _GENERIC_HEADLINE.match(live_headline), (
                f"the live error pill headlined the connection failure generically: {observed}"
            )
            assert not _GENERIC_HEADLINE.match(reloaded_headline), (
                f"the reloaded error pill headlined the failure generically: {observed}"
            )
            assert last_error["code"] == "connection_error", (
                f"the session's failure code lost the connection-error classification: {observed}"
            )
            assert item_codes and item_codes == ["connection_error"] * len(item_codes), (
                f"the persisted error item lost the connection-error classification: {observed}"
            )
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:
                respawned.kill()
                respawned.wait(timeout=5)
