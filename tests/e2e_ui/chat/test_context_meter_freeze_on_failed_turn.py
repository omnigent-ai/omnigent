"""UI journey: the context-occupancy ring must not freeze when a turn fails.

The claude-sdk executor observes the turn's
prompt usage mid-turn (each Anthropic ``message_start`` carries it) but only
emits ``context_tokens`` when the turn completes successfully. A turn that
fails after the model call started (stream death, auth failure, the 240s
watchdog) discards that observed usage, so the UI's context ring stays frozen
at the previous successful turn's value — showing a misleadingly low fill
exactly when the session is in trouble.

Journey driven here, on the real web SPA against a live server + runner and
the real claude CLI pointed at the mock Anthropic endpoint:

1. start a claude-sdk session (spec declares a 200K context window)
2. send a message; the turn succeeds with a small prompt (2,000 tokens)
   → the ring shows ~1%
3. send a second message; the model call STARTS and reports a large prompt
   (100,000 tokens) in ``message_start``, then the turn fails
   (the stream dies mid-flight and the retry is rejected with 401)
4. observable failure: the ring still shows the stale ~1% instead of the
   large fill the harness already observed on the failed turn

Regression guard: the final assertion (ring reflects the failed turn's
observed usage) FAILS on the current build and passes once failed turns
propagate their observed ``context_tokens``.
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

# Spec-declared window; percentages below derive from it.
_CONTEXT_WINDOW = 200_000
# Turn 1 prompt usage → 2000/200000 = 1%.
_TURN1_INPUT_TOKENS = 2_000
# Turn 2 prompt usage — large enough that any effective window resolves
# it to a 40-99% fill, far from turn 1's single digits.
_TURN2_INPUT_TOKENS = 100_000


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
    name = f"ctx-freeze-{uuid.uuid4().hex[:8]}"
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
def test_context_ring_updates_when_turn_fails_after_usage_observed(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A failed turn's observed prompt usage must still reach the context ring.

    The mock scripts turn 2 to open normally (its ``message_start`` reports a
    100K-token prompt) and then die mid-stream; the claude CLI's retry is
    rejected with 401, so the turn fails. The executor observed the 100K
    prompt before the failure, so the ring must show a large fill — pre-fix
    it stays frozen at turn 1's 1%.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_claude_sdk_session(live_server, runner_id, mock_llm_server_url)
        try:
            uid = uuid.uuid4().hex[:6]
            # Equal-length match tokens: the mock routes by the LONGEST match,
            # then the RIGHTMOST occurrence in the (cumulative) conversation
            # the CLI resends — so turn 2's calls route to turn 2's queue even
            # though turn 1's token is also present in the transcript.
            token1 = f"ctxmeter-one-{uid}"
            token2 = f"ctxmeter-two-{uid}"

            # Turn 1: every API call this turn makes (the CLI can add
            # follow-up calls, e.g. skills/system-reminder) sees the same
            # 2,000-token prompt usage, so the LAST observed call — which is
            # what context_tokens reports — is deterministic at 1%.
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "ack", "usage": {"input_tokens": _TURN1_INPUT_TOKENS}}] * 4,
                key=f"ctx-turn1-{uid}",
                match=token1,
            )
            # Turn 2: the opening calls stream normally — their message_start
            # reports the 100K prompt — then die after 2 events (no
            # completion). Every retry the CLI attempts is rejected 401, which
            # the executor treats as terminal — the turn fails AFTER its
            # prompt usage was observed — the freeze shape. Two truncated
            # entries, not one: the CLI opens parallel API calls at turn
            # start, and only the main call's stream events reach the
            # executor. With a single entry the side call can consume it,
            # leaving the main call to hit a 401 before ever reporting the
            # 100K prompt — which tests a different (no-usage-observed)
            # failure than the one this journey pins.
            configure_mock_llm(
                mock_llm_server_url,
                [
                    {
                        "text": "never delivered",
                        "usage": {"input_tokens": _TURN2_INPUT_TOKENS},
                        "truncate_after": 2,
                    },
                ]
                * 2
                + [{"error": "authentication_failed", "status_code": 401}] * 8,
                key=f"ctx-turn2-{uid}",
                match=token2,
            )

            page.goto(f"{live_server}/c/{session_id}")

            # ── Turn 1: succeeds, ring shows the small fill ─────────────
            _send(page, f"Say ack. {token1}")
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=120_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)
            ring = page.locator(_RING)
            expect(ring).to_be_visible(timeout=30_000)
            # The CLI can make more than one internal API call per turn, so
            # the exact percentage varies slightly (1-2%); what matters is
            # that turn 1 settled at a SMALL fill, far from turn 2's.
            expect(ring).to_have_attribute(
                "aria-label",
                re.compile(r"^[0-9]% of context used$"),
                timeout=30_000,
            )

            # ── Turn 2: model call starts (100K prompt observed), then the
            # turn fails. Wait for the turn to settle as failed: the error
            # pill renders from the response.failed event.
            _send(page, f"Continue. {token2}")
            expect(page.locator('[data-testid="error-pill"]').first).to_be_visible(timeout=240_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

            # ── The reproduction assertion: the harness observed a
            # 100K-token prompt on the failed turn, so the ring must reflect
            # that large fill — NOT stay frozen at turn 1's single-digit
            # percentage. A tolerant 40-99% band keeps the assertion pinned
            # to "the failed turn's observed usage arrived" without
            # over-fitting how the stack resolves the effective context
            # window (spec-declared 200K → 50%; catalog/default 128K → 78%).
            expect(ring).to_have_attribute(
                "aria-label",
                re.compile(r"^[4-9][0-9]% of context used$"),
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
