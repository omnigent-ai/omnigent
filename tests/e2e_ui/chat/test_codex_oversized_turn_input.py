"""UI journey: an oversized Codex message must fail with a clear reason, not a raw error dump.

The codex app-server rejects a ``turn/start`` whose input exceeds 1,048,576
characters with JSON-RPC ``-32602`` (``input_too_large``). The SDK harness
used to surface that as an uncoded ``Codex executor error: {...}`` string, so
the runner published a generic code (``runner_error``, later ``RuntimeError``)
and the SPA showed a generic headline over the raw dict.

Journey (real web SPA, live server + runner, real ``codex`` CLI app-server):

1. open a fresh headless ``codex`` (SDK-mode) session
2. paste a ~1.45M-character message into the composer and click Send
3. the turn fails; the error pill must explain that the message exceeds
   Codex's input limit instead of showing the generic headline and raw dict,
   and the session's recorded error must carry the ``input_too_large`` code
"""

from __future__ import annotations

import re
import shutil
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state
from tests.e2e_ui.sessions.test_codex_multistep_turn_usage import _build_codex_bundle

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="the codex CLI binary is required to spawn the SDK harness's app-server",
)

_CODEX_MAX_INPUT_CHARS = 1_048_576
# Near the 1,450,257-character turn the ticket's log line reported.
_TARGET_INPUT_CHARS = 1_450_000
_LOG_LINE = "2026-09-09T14:12:42.117Z INFO worker-7 heartbeat ok latency_ms=12\n"
_GENERIC_HOST_HEADLINE = "Something went wrong setting up the turn on the host."
_RAW_RPC_FRAGMENTS = ("-32602", "input_error_code", "Codex executor error:")
_INPUT_LIMIT_REFERENCE = re.compile(r"1,?048,?576|too large|input_too_large", re.IGNORECASE)
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_TURN_SETTLE_TIMEOUT_S = 240


def _create_codex_session(base_url: str, runner_id: str) -> str:
    """Create a runner-bound session for a fresh headless-``codex`` agent."""
    name = f"codex-oversized-{uuid.uuid4().hex[:8]}"
    # A preset title keeps background title inference away from the mock model.
    create = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        _build_codex_bundle(name, f"mock-{name}"),
        metadata={"title": "Codex oversized input"},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _oversized_message() -> str:
    """A pasted log past Codex's turn-input ceiling."""
    return "Summarize the errors in this log:\n" + _LOG_LINE * (
        _TARGET_INPUT_CHARS // len(_LOG_LINE)
    )


def _paste_into_composer(page: Page, text: str) -> None:
    # fill() stalls on a >1 MiB controlled textarea; set the value directly and notify React.
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_enabled(timeout=60_000)
    composer.click()
    composer.evaluate(
        "(el, text) => {"
        " const setter = Object.getOwnPropertyDescriptor("
        "HTMLTextAreaElement.prototype, 'value').set;"
        " setter.call(el, text);"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        "}",
        text,
    )


def _wait_for_turn_settled(base_url: str, session_id: str, timeout_s: int) -> dict:
    """Return the session snapshot once the turn has left idle and then settled."""
    deadline = time.monotonic() + timeout_s
    entered_active = False
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        status = snapshot.get("status")
        if status in ("running", "waiting"):
            entered_active = True
        elif status == "failed" or (status == "idle" and entered_active):
            return snapshot
        time.sleep(1.0)
    raise AssertionError(
        f"turn did not settle within {timeout_s}s: status={snapshot.get('status')!r}"
    )


def _surfaced_error_texts(page: Page) -> tuple[list[str], list[str]]:
    """Expand every error pill and return its headlines and message bodies."""
    pills = page.get_by_test_id("error-pill")
    expect(pills.first).to_be_visible(timeout=30_000)
    headlines: list[str] = []
    bodies: list[str] = []
    for index in range(pills.count()):
        pill = pills.nth(index)
        headline = pill.get_by_test_id("error-headline")
        headlines.append(headline.inner_text())
        headline.click()
        content = pill.get_by_test_id("error-message-content")
        expect(content).to_be_visible(timeout=10_000)
        bodies.append(content.inner_text())
    return headlines, bodies


@pytest.mark.timeout(600)
def test_codex_oversized_message_fails_with_clear_reason(
    request: pytest.FixtureRequest,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An over-limit codex message must not surface as a generic host error over a raw dict."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_codex_session(live_server, runner_id)
        # Delete during teardown, after the recorded context has closed on the failure state.
        request.addfinalizer(
            lambda: httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        )
        # Request the recorded page only after setup so footage starts at the journey.
        page: Page = request.getfixturevalue("page")
        print(f"codex oversized-input session: {session_id}")
        page.goto(f"{live_server}/c/{session_id}")

        message = _oversized_message()
        assert len(message) > _CODEX_MAX_INPUT_CHARS
        _paste_into_composer(page, message)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator(_USER_BUBBLE)).to_have_count(1, timeout=60_000)

        snapshot = _wait_for_turn_settled(live_server, session_id, _TURN_SETTLE_TIMEOUT_S)
        expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
        last_error = snapshot.get("last_task_error") or {}
        if snapshot.get("status") != "failed" and not last_error:
            # A build that accepts or trims the oversized input completes the turn.
            expect(page.locator(_ASSISTANT_BUBBLE).first).to_be_visible(timeout=30_000)
            return

        headlines, bodies = _surfaced_error_texts(page)
        surfaced = [*headlines, *bodies, str(last_error.get("message", ""))]
        # Precondition: the failure is the input-size rejection, not an unrelated launch error.
        assert any(_INPUT_LIMIT_REFERENCE.search(text) for text in surfaced), (
            f"the turn failed for a reason unrelated to Codex's input limit: {surfaced!r}"
        )

        assert _GENERIC_HOST_HEADLINE not in headlines, (
            f"oversized input reported as a generic host-setup failure: {surfaced!r}"
        )
        for fragment in _RAW_RPC_FRAGMENTS:
            assert all(fragment not in text for text in surfaced), (
                f"raw JSON-RPC fragment {fragment!r} reached the user: {surfaced!r}"
            )
        assert last_error.get("code") == "input_too_large", (
            f"over-limit rejection recorded under a generic code: {last_error!r}"
        )
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
