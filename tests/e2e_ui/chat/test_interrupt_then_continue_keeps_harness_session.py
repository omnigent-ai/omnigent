"""UI journey: after Interrupt, a follow-up must continue the same harness session.

Real web SPA, live server and runner, real codex / claude / pi CLIs against the
mock model. Turn 1 completes, turn 2 is interrupted from the composer while its
model reply is still in flight, then turn 3 asks the agent to continue. A harness that started over
sends its first model request as a ``Conversation so far:`` transcript replay;
a continued session sends only the new message, led by the interruption marker.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import uuid

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_FRESH_SESSION_REPLAY = "Conversation so far:"
_INTERRUPTION_MARKER = "[System: interrupted]"
_HARNESS_CLI = {"codex": "codex", "claude-sdk": "claude", "pi": "pi"}
_LONG_REPLY = " ".join(f"section{index:02d}" for index in range(1, 61))
# The mock sends Messages and Chat Completions text in one delta, so hold those replies instead.
_INTERRUPTED_REPLY = {
    "codex": {"text": _LONG_REPLY, "stream": True, "chunk_delay": 0.4},
    "claude-sdk": {"text": _LONG_REPLY, "delay": 90},
    "pi": {"text": _LONG_REPLY, "delay": 90},
}


def _executor(harness: str, model: str, mock_llm_server_url: str) -> dict:
    if harness == "claude-sdk":
        return {
            "harness": "claude-sdk",
            "model": model,
            "context_window": 200000,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_server_url},
        }
    return {"harness": harness, "model": model}


def _create_session(base_url: str, runner_id: str, executor: dict) -> str:
    name = f"interrupt-continue-{uuid.uuid4().hex[:8]}"
    spec = {"name": name, "prompt": "You are a terse assistant.", "executor": executor}
    bundle = bundle_files({f"{name}.yaml": yaml.safe_dump(spec, sort_keys=False).encode()})
    created = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        bundle,
        metadata={"title": "Interrupt then continue"},
        timeout=30.0,
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _delete_session(base_url: str, session_id: str) -> None:
    with contextlib.suppress(httpx.HTTPError):  # best-effort cleanup
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_editable(timeout=60_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _requests_for_turn(mock_llm_server_url: str, token: str, later_tokens: list[str]) -> list[str]:
    response = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    response.raise_for_status()
    bodies = [json.dumps(request) for request in response.json()["requests"]]
    return [body for body in bodies if token in body and not any(t in body for t in later_tokens)]


@pytest.mark.timeout(600)
@pytest.mark.parametrize("harness", ["codex", "claude-sdk", "pi"])
def test_follow_up_after_interrupt_continues_harness_session(
    request: pytest.FixtureRequest,
    harness: str,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    if shutil.which(_HARNESS_CLI[harness]) is None:
        pytest.skip(f"the {_HARNESS_CLI[harness]} CLI is required for the {harness} harness")
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    uid = uuid.uuid4().hex[:6]
    model = "claude-sonnet-4-20250514" if harness == "claude-sdk" else f"mock-interrupt-{uid}"
    tokens = [f"{uid}turn1", f"{uid}turn2", f"{uid}turn3"]
    replies = [
        {"text": "Ready to write the report."},
        _INTERRUPTED_REPLY[harness],
        {"text": "Continuing the report now."},
    ]
    # Spare entries absorb the claude CLI's side calls that also carry the user text.
    for token, reply in zip(tokens, replies, strict=True):
        configure_mock_llm(mock_llm_server_url, [reply] * 3, match=token)

    session_id = _create_session(
        live_server, runner_id, _executor(harness, model, mock_llm_server_url)
    )
    request.addfinalizer(lambda: _delete_session(live_server, session_id))
    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/c/{session_id}")
    assistant = page.locator(_ASSISTANT)
    interrupt = page.get_by_role("button", name="Interrupt", exact=True)

    _send(page, f"Get ready to write a long report. {tokens[0]}")
    expect(assistant.filter(has_text="Ready to write the report.")).to_be_visible(timeout=180_000)
    expect(interrupt).to_be_hidden(timeout=60_000)

    _send(page, f"Write the long report now. {tokens[1]}")
    expect(interrupt).to_be_visible(timeout=60_000)
    poll_until(lambda: _requests_for_turn(mock_llm_server_url, tokens[1], []), timeout_s=60)
    if harness == "codex":
        expect(assistant.filter(has_text="section04")).to_be_visible(timeout=60_000)
    interrupt.click()
    expect(interrupt).to_be_hidden(timeout=10_000)

    _send(page, f"Please continue where you left off. {tokens[2]}")
    expect(assistant.filter(has_text="Continuing the report now.")).to_be_visible(timeout=180_000)

    interrupted_turn = _requests_for_turn(mock_llm_server_url, tokens[1], tokens[2:])
    assert interrupted_turn, f"no model request captured for the interrupted {harness} turn"
    assert not any(_FRESH_SESSION_REPLAY in body for body in interrupted_turn), (
        f"precondition: {harness} replayed history on a normal second turn"
    )
    continued_turn = _requests_for_turn(mock_llm_server_url, tokens[2], [])
    assert continued_turn, f"no model request captured for the {harness} follow-up turn"
    replays = [
        body[body.index(_FRESH_SESSION_REPLAY) :]
        for body in continued_turn
        if _FRESH_SESSION_REPLAY in body
    ]
    assert not replays, (
        f"after Interrupt the {harness} follow-up started a fresh harness session: its model "
        f"request replays the transcript instead of continuing the session: {replays[0][:900]}"
    )
    marked = [
        body
        for body in continued_turn
        if body.count(_INTERRUPTION_MARKER) == 1
        and body.index(_INTERRUPTION_MARKER) < body.index(tokens[2])
    ]
    assert marked and all(body.count(_INTERRUPTION_MARKER) <= 1 for body in continued_turn), (
        f"the {harness} follow-up must carry the interruption marker once, before the new input"
    )
