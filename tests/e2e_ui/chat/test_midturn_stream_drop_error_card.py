"""Claude stops mid-response, then a delayed *generic* runtime error.

Reported journey (Omnigent desktop, a Claude session):

1. open a session and send a prompt
2. Claude starts responding (text streams in)
3. the model stream dies mid-flight — the answer stops with no message/error,
   yet the host is still connected
4. after waiting a few seconds a runtime-error card appears

Reported impact: "User is confused if Claude is still running or not. Not sure
what the issue is." — i.e. the turn stops silently and the error that finally
appears is a *generic* card that never names what actually went wrong (the
model stream dropped mid-turn), so the user cannot tell a dropped connection
from a still-running turn.

Driven here on the real web SPA against a live server + runner and the real
claude CLI pointed at the mock Anthropic endpoint. The mock scripts the user
turn's model calls — routed by a content token so only this turn is affected —
to open a normal SSE stream, emit a couple of text deltas, then die mid-flight
without a ``message_stop``. Every retry the CLI attempts also dies mid-stream,
so the turn fails as a dropped-connection error after it visibly started. The
runner is never killed, so the host stays online the whole time (matching
"Host also still seems connected").

Regression guard: a mid-turn stream drop must surface as a card that *names*
the failure (a dropped/lost connection to the agent), NOT the generic
catch-all "Something went wrong" (nor the ``runner_error`` "setting up the turn
on the host" misattribution). On the current build the card headline is the
bare "Something went wrong" fallback — the final assertion FAILS there and
passes once the mid-turn failure is classified for the user.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_HEADLINE = '[data-testid="error-headline"]'
_ERROR_MESSAGE = '[data-testid="error-message-content"]'
_DISCONNECTED = '[data-testid="disconnected-indicator"]'

# The generic catch-all headline the bug leaves the user with, and the
# runner_error misattribution the underlying defect can also produce. Neither
# names the real cause (the mid-turn stream drop).
_GENERIC = "Something went wrong"
_MISATTRIBUTED = "Something went wrong setting up the turn on the host"


def _build_claude_sdk_bundle(name: str, mock_llm_server_url: str) -> bytes:
    """A one-file claude-sdk agent bundle wired at the mock Anthropic endpoint."""
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "claude-sdk",
            "model": "claude-sonnet-4-20250514",
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


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_url: str) -> str:
    name = f"middrop-{uuid.uuid4().hex[:8]}"
    bundle = _build_claude_sdk_bundle(name, mock_url)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    ).raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(600)
def test_midturn_stream_death_surfaces_named_error_not_generic(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A turn whose model stream dies mid-flight must end in a runtime-error
    card that *names* the dropped connection (host still online), not the
    generic "Something went wrong" catch-all that leaves the user guessing."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_claude_sdk_session(live_server, runner_id, mock_llm_server_url)
        try:
            uid = uuid.uuid4().hex[:6]
            token = f"middrop-{uid}"

            # Route ONLY this turn's model calls (the token rides in the user
            # text the CLI resends on every call) to a queue where EVERY entry
            # opens a normal SSE stream, emits 2 text deltas, then dies
            # mid-flight (no message_stop). The main call and every retry the
            # CLI makes all drop mid-stream, so the turn fails as a
            # dropped-connection error AFTER Claude visibly started responding
            # — never a clean completion. Plenty of entries so no retry runs
            # off the end of the queue. Warmup / system-only calls (no user
            # text) never match this token, so they are unaffected.
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "Sure, here is the history of computing", "truncate_after": 2}] * 12,
                key=f"middrop-{uid}",
                match=token,
            )

            page.goto(f"{live_server}/c/{session_id}")
            _send(page, f"Write a 500-word essay about the history of computing. {token}")

            # The turn settles as failed: a terminal error card renders from
            # the response.failed / failed-status edge after the mid-stream drop.
            error_pill = page.locator(_ERROR_PILL).first
            expect(error_pill).to_be_visible(timeout=240_000)

            # The "Working…" shimmer must not linger forever once the turn has
            # terminally failed — the reported confusion is Claude appearing to
            # still run after it has stopped.
            expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

            # The host/runner never died — it stays online throughout (the
            # report notes "Host also still seems connected"), so the failure
            # must NOT be rendered as a host disconnect.
            expect(page.locator(_DISCONNECTED)).to_have_count(0)
            health = httpx.get(f"{live_server}/health?session_id={session_id}", timeout=5.0).json()
            assert health.get("session", {}).get("runner_online") is True, (
                f"runner should still be online for a mid-turn model failure, got: {health}"
            )

            # Read the card. The headline (always visible) carries the
            # code→text classification; expand for the message body too.
            headline_loc = page.locator(_ERROR_HEADLINE).first
            headline = headline_loc.inner_text()
            error_pill.click()
            try:
                message = page.locator(_ERROR_MESSAGE).first.inner_text(timeout=5_000)
            except Exception:
                message = ""
            print(f"\nerror card headline: {headline!r}")
            print(f"error card message:  {message!r}")

            # The reproduction assertion: a mid-turn stream drop is a
            # connection failure, so the card must name it — never the generic
            # "Something went wrong" catch-all, and never the runner_error
            # "setting up the turn on the host" misattribution. Both leave the
            # user unable to tell a dropped connection from a running turn.
            expect(headline_loc).not_to_contain_text(_MISATTRIBUTED)
            expect(headline_loc).not_to_have_text(_GENERIC)
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
