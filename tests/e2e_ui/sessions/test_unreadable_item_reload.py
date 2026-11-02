"""Verify that a rejected item payload does not prevent a conversation from reloading.

The spawned server's decode seam (``SqlAlchemyConversationStore._decode_item_data_batch``)
raises the gRPC ``RESOURCE_EXHAUSTED`` a decrypt sidecar returns for an oversized frame
whenever a page holds the marked message. The reload must still render the readable
items, show that message as an expandable placeholder, and leave the composer usable.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, expect

from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle, configure_mock_llm

MODEL_KEY = "gpt-4o-mini"
MARKER = "oversized-attachment-stand-in"
MESSAGE = f"Please read this PDF. {MARKER}"
REPLY = "I received your PDF."
_COMPOSER_LABEL = "Message the agent"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_PLACEHOLDER_TITLE = "Part of this conversation couldn't be loaded"
_PLACEHOLDER_DETAIL = "Its stored content could not be read"

# Runs inside the spawned server before the CLI starts (``python -c`` bootstrap).
SERVER_BOOTSTRAP = f"""
import grpc
from omnigent.stores.conversation_store import sqlalchemy_store as _conv

_MARKER = {MARKER!r}


class _SidecarRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.RESOURCE_EXHAUSTED

    def details(self):
        return "Frame size 3547826 exceeds maximum: 1048576"


def _decode_item_data_batch(self, stored):
    if any(_MARKER in (s if isinstance(s, str) else s.decode()) for s in stored):
        raise _SidecarRpcError("RPC terminated with RESOURCE_EXHAUSTED")
    return stored


_conv.SqlAlchemyConversationStore._decode_item_data_batch = _decode_item_data_batch

from omnigent.cli import main

main()
"""


def create_bound_session(client: httpx.Client, stack: ServerRunner) -> str:
    create = post_session_bundle(
        client.post, f"{stack.base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id, timeout=10.0)
    return session_id


def send_message(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    send = page.get_by_role("button", name="Send", exact=True)
    expect(send).to_be_enabled(timeout=10_000)
    send.click()
    expect(page.locator(_USER).first).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)


def test_session_with_unreadable_item_reopens_with_placeholder(
    browser: Browser, mock_llm_server_url: str, tmp_path: Path
) -> None:
    """Send the marked message, reload: the conversation loads again (no 500) with
    the reply visible and an expandable placeholder for the message the store refuses."""
    provider_env = {"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"}
    with (
        httpx.Client(trust_env=False) as client,
        server_runner(
            tmp_path / "stack", server_bootstrap=SERVER_BOOTSTRAP, server_env=provider_env
        ) as stack,
    ):
        stack.start_runner(env=provider_env)
        configure_mock_llm(mock_llm_server_url, [{"text": REPLY}] * 3, key=MODEL_KEY)
        session_id = create_bound_session(client, stack)
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(f"{stack.base_url}/c/{session_id}")
            expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
            send_message(page, MESSAGE)
            with page.expect_response(
                lambda response: f"/v1/sessions/{session_id}/items" in response.url,
                timeout=30_000,
            ) as reloaded:
                page.reload()
            response = reloaded.value
            body = response.text()
            assert response.status == 200, (
                f"GET /items after reload answered {response.status}: {body[:300]}"
            )
            kinds = [
                (item["type"], item.get("code") or item.get("role"))
                for item in json.loads(body)["data"]
            ]
            assert kinds.count(("error", "item_data_unreadable")) == 1, kinds
            assert ("message", "assistant") in kinds, kinds

            expect(page.get_by_role("heading", name="Conversation not found")).to_have_count(0)
            expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
            expect(page.get_by_text(REPLY).first).to_be_visible()
            placeholder = page.get_by_role("button", name=_PLACEHOLDER_TITLE)
            expect(placeholder).to_be_visible()
            placeholder.click()
            expect(page.get_by_text(_PLACEHOLDER_DETAIL).first).to_be_visible()
        finally:
            context.close()
