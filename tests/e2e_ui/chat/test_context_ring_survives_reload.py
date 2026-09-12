"""UI journey: the context-occupancy ring must survive a page refresh.

For relay harnesses (claude-sdk — what polly's orchestrator brain runs on),
the turn's ``context_tokens`` arrive on the live ``response.completed`` SSE
event; the server must also persist them to the
``omnigent.last_context_tokens`` label the session snapshot reads
``last_total_tokens`` from. A relay that skips that persistence leaves a
refreshed page's snapshot seeding ``tokensUsed`` as null, the composer status
line's ``showRing`` guard fails, and the context ring vanishes until the NEXT
turn completes — even though the session has already consumed context.

Journey driven here, on the real web SPA against a live server + runner and
the real claude CLI pointed at the mock Anthropic endpoint:

1. start a claude-sdk session (spec declares a 200K context window)
2. send a message; the turn succeeds → the ring shows a small fill
3. refresh the chat page and wait for the transcript to hydrate
4. regression guard: the context ring is still visible, with a percentage,
   right after the reload — without sending another message

A relay that only delivers context tokens on the live event, without
persisting them for the session snapshot, fails the final assertion.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import uuid

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
# The composer status tray's context ring exposes its value via aria-label,
# e.g. "1% of context used".
_RING = '[data-testid="composer-status-line"] [aria-label$="of context used"]'

# Spec-declared window; the ring's percentage derives from it.
_CONTEXT_WINDOW = 200_000
# Turn 1 prompt usage → a small single-digit fill.
_TURN1_INPUT_TOKENS = 2_000


def _build_claude_sdk_bundle(name: str, mock_llm_server_url: str) -> bytes:
    """Build a one-file claude-sdk agent bundle wired at the mock LLM.

    Mirrors ``tests/e2e/conftest.register_inline_agent``'s claude-sdk shape:
    ``executor.auth`` (type api_key + base_url) routes the claude CLI's
    ``ANTHROPIC_BASE_URL`` at the mock server, which serves the Anthropic
    ``/v1/messages`` SSE format. ``context_window`` is declared so the SPA's
    context ring renders with a known denominator.

    :param name: Agent name (unique per test run).
    :param mock_llm_server_url: Mock server base URL WITHOUT ``/v1`` (the
        Anthropic SDK appends ``/v1/messages`` itself).
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "claude-sdk",
            "model": "claude-sonnet-4-20250514",
            "context_window": _CONTEXT_WINDOW,
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_server_url,
            },
        },
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_llm_server_url: str) -> str:
    """Create a runner-bound session for a fresh claude-sdk agent.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :param mock_llm_server_url: Mock server base URL (no ``/v1``).
    :returns: The new session id.
    """
    name = f"ctx-reload-{uuid.uuid4().hex[:8]}"
    bundle = _build_claude_sdk_bundle(name, mock_llm_server_url)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(600)
def test_context_ring_survives_page_reload(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The context ring must still render right after a page refresh.

    Turn 1 succeeds and the ring shows a small fill from the turn's live
    ``response.completed`` usage. A reload then rebuilds the page purely from
    the session snapshot — which must carry the same context occupancy, so
    the ring is visible again without sending another message. A relay that
    never persists its observed context tokens leaves the snapshot without
    ``last_total_tokens`` and the ring stays hidden after the refresh.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_claude_sdk_session(live_server, runner_id, mock_llm_server_url)
        try:
            uid = uuid.uuid4().hex[:6]
            token1 = f"ctxreload-one-{uid}"

            # Every API call this turn makes (the CLI can add follow-up
            # calls, e.g. skills/system-reminder) sees the same 2,000-token
            # prompt usage, so the LAST observed call — which is what
            # context_tokens reports — is deterministic at a small fill.
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "ack", "usage": {"input_tokens": _TURN1_INPUT_TOKENS}}] * 4,
                key=f"ctx-reload-{uid}",
                match=token1,
            )

            page.goto(f"{live_server}/c/{session_id}")

            # ── Turn 1: succeeds, ring shows a small fill ────────────────
            _send(page, f"Say ack. {token1}")
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=120_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)
            ring = page.locator(_RING)
            expect(ring).to_be_visible(timeout=30_000)
            expect(ring).to_have_attribute(
                "aria-label",
                re.compile(r"^[0-9]+% of context used$"),
                timeout=30_000,
            )

            # ── Refresh: the journey step that used to lose the ring ─────
            page.reload()
            # The transcript hydrates from the snapshot: turn 1's bubble is
            # back and the composer is interactive, so the status line has
            # everything it will ever get without a new message.
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
            expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

            # ── The regression assertion: the ring must still be there.
            # The session's context occupancy did not change across the
            # reload, so the snapshot-seeded status line must render the
            # ring with a percentage — without sending another message.
            expect(ring).to_be_visible(timeout=15_000)
            expect(ring).to_have_attribute(
                "aria-label",
                re.compile(r"^[0-9]+% of context used$"),
                timeout=15_000,
            )
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
