"""A Claude Code prompt-too-long rejection must surface as a context-window overflow.

Drives the claude-sdk harness (real Claude Code CLI) on the SPA against the mock
Anthropic endpoint, which answers every request with HTTP 400 "prompt is too
long". Claude Code fails to compact, and each later wake of the session (an
orchestrator heartbeat posted through the sessions API, then a bare "go") fails
the same way. Every failure card must read as a context-window overflow.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.chat.test_model_unavailable_recovery_hint import (
    _delete_session,
    _stop_respawned_runner,
    _type_and_submit,
)
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ASSISTANT, _USER

_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_OVERFLOW = "prompt is too long: 1006037 tokens > 1000000 maximum"
_HEARTBEAT = "You are idle with asks still open - continue the open work."
_OVERFLOW_HEADLINE = "The conversation grew past the model's context window."
_TURN_TIMEOUT_S = 150.0
# Readable pauses for filmed runs only.
_HOLD_MS = 4_000 if os.environ.get("OMNIGENT_E2E_RECORD_DIR") else 0


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_url: str) -> str:
    name = f"overflow-{uuid.uuid4().hex[:8]}"
    executor: dict[str, object] = {"harness": "claude-sdk", "model": _CLAUDE_MOCK_MODEL}
    # A workflow-owned runner already routes to the mock and cannot reach this URL.
    if not _server_state.get("workflow_owned"):
        executor["auth"] = {"type": "api_key", "api_key": "mock-key", "base_url": mock_url}
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": executor,
    }
    bundle = bundle_files({f"{name}.yaml": yaml.safe_dump(config, sort_keys=False).encode()})
    created = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _snapshot(base_url: str, session_id: str) -> dict[str, object]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json()


def _post_heartbeat(base_url: str, session_id: str) -> None:
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": _HEARTBEAT}]},
        },
        timeout=15.0,
    ).raise_for_status()


def _wait_for_failure_card(
    page: Page, base_url: str, session_id: str, pills_before: int, mark: Callable[[str], None]
) -> dict:
    pills = page.get_by_test_id("error-pill")
    deadline = time.monotonic() + _TURN_TIMEOUT_S
    while pills.count() <= pills_before:
        if time.monotonic() > deadline:
            raise AssertionError(f"no new error card; session {_snapshot(base_url, session_id)}")
        page.wait_for_timeout(1_000)
    card = pills.last
    card.scroll_into_view_if_needed()
    headline = card.get_by_test_id("error-headline")
    expect(headline).to_be_visible(timeout=15_000)
    mark("card visible")
    page.wait_for_timeout(_HOLD_MS)
    toggle = card.locator('button[aria-expanded="false"]')
    if toggle.count():
        toggle.first.click()
    detail = card.get_by_test_id("error-message-content")
    expect(detail).to_be_visible(timeout=15_000)
    detail.scroll_into_view_if_needed()
    mark("card expanded")
    page.wait_for_timeout(_HOLD_MS)
    return {
        "headline": headline.inner_text(),
        "detail": detail.inner_text(),
    }


@pytest.mark.timeout(600)
def test_claude_sdk_prompt_too_long_cards_read_as_context_overflow(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    session_id = ""
    try:
        session_id = _create_claude_sdk_session(
            live_server, str(_server_state["runner_id"]), mock_llm_server_url
        )
        print(f"claude-sdk session: {session_id}", flush=True)
        reset_mock_llm(mock_llm_server_url)
        # Claude Code's background title request can draw from the same queue.
        configure_mock_llm(mock_llm_server_url, [{"text": "Ready."}] * 4, key=_CLAUDE_MOCK_MODEL)

        page: Page = request.getfixturevalue("page")
        started = time.monotonic()

        def mark(label: str) -> None:
            print(f"t={time.monotonic() - started:6.1f}s {label}", flush=True)

        page.goto(f"{live_server}/c/{session_id}")
        mark("opened session")
        _type_and_submit(page, "Start the review checklist.")
        expect(page.locator(_ASSISTANT, has_text="Ready.")).to_be_visible(
            timeout=int(_TURN_TIMEOUT_S * 1000)
        )
        mark("healthy reply shown")
        page.wait_for_timeout(_HOLD_MS)

        overflow = [{"error": _OVERFLOW, "status_code": 400}] * 200
        configure_mock_llm(mock_llm_server_url, overflow, key=_CLAUDE_MOCK_MODEL)
        wakes: list[tuple[str, str, Callable[[], None]]] = [
            (
                "message",
                "Continue the open work.",
                lambda: _type_and_submit(page, "Continue the open work."),
            ),
            ("heartbeat", _HEARTBEAT, lambda: _post_heartbeat(live_server, session_id)),
            ("go", "go", lambda: _type_and_submit(page, "go")),
        ]
        cards = []
        for label, text, wake in wakes:
            pills_before = page.get_by_test_id("error-pill").count()
            mark(f"{label} sent")
            wake()
            expect(page.locator(_USER, has_text=text).last).to_be_visible(timeout=30_000)
            mark(f"{label} shown in chat")
            card = _wait_for_failure_card(page, live_server, session_id, pills_before, mark)
            mark(f"{label} card: {card}")
            cards.append(card)

        snapshot = _snapshot(live_server, session_id)
        mark("journey complete")

        assert [card["headline"] for card in cards] == [_OVERFLOW_HEADLINE] * len(cards), cards
        # Deliberately covers the pinned CLI's rewrite of the mock's pair into the plain and
        # approximate forms users see; neither states an explicit pair, so cards keep the text.
        assert all("Prompt is too long" in card["detail"] for card in cards), cards
        assert any("(limit 1000000)" in card["detail"] for card in cards), cards
        last_error = snapshot.get("last_task_error") or {}
        assert last_error.get("code") == "context_length_exceeded", last_error
    finally:
        if session_id:
            _delete_session(live_server, session_id)
        _stop_respawned_runner(respawned)
